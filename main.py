# main.py
# Punto de entrada del scraper de LinkedIn.
#
# Modos:
#   --mode index   → Fase A: recopila todos los slugs de conexiones y los
#                    guarda en contact_queue como 'pending'. Rápido.
#   --mode enrich  → Fase B (defecto): toma slugs 'pending' de la queue,
#                    visita cada perfil, extrae datos completos y los guarda
#                    en la tabla contacts. Respetar límites anti-ban.
#
# Controles de seguridad activos en ambos modos:
#   · Cooldown tras bloqueo (429 / on_block)
#   · Franja horaria (SCRAPE_WINDOW_START – SCRAPE_WINDOW_END)
#   · Presupuesto diario (MAX_CONTACTS_PER_DAY)
#   · Intervalo mínimo entre ejecuciones (MIN_HOURS_BETWEEN_RUNS)

import argparse
import logging
import os
import re
import sys
import time
import pandas as pd
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv

from scraper import (
    init_client,
    get_current_username,
    scrape_profile_and_connections,
    collect_all_slugs,
    _enrich_connection_from_profile,
    _create_driver_with_cookies,
    session_file_for,
)
from db import (
    insert_run,
    queue_slugs,
    get_pending_slugs,
    upsert_contact,
    mark_queue_done,
    mark_queue_error,
    get_daily_count,
    get_queue_stats,
    contact_exists,
    days_since_last_scrape,
    register_account,
    update_account_last_run,
    get_account_proxy,
)
from notifications import (
    notify_session_expired,
    notify_block,
    notify_daily_summary,
    notify_index_complete,
)
from log_config import setup_logging

load_dotenv()

logger = logging.getLogger(__name__)

# ── Configuración desde .env ───────────────────────────────────────────────────

COOLDOWN_HOURS_AFTER_429  = int(os.getenv("COOLDOWN_HOURS_AFTER_429", "48"))
MIN_HOURS_BETWEEN_RUNS    = int(os.getenv("MIN_HOURS_BETWEEN_RUNS", "0"))
MAX_CONTACTS_PER_RUN      = max(1, min(int(os.getenv("MAX_CONTACTS_PER_RUN",
                                           os.getenv("MAX_CONTACTS", "20"))), 50))
MAX_CONTACTS_PER_DAY      = max(1, int(os.getenv("MAX_CONTACTS_PER_DAY", "80")))
SCRAPE_WINDOW_START       = int(os.getenv("SCRAPE_WINDOW_START", "8"))   # hora (0-23)
SCRAPE_WINDOW_END         = int(os.getenv("SCRAPE_WINDOW_END", "21"))    # hora (0-23)
# Días mínimos antes de refrescar un contacto ya scrapeado.
# Si tiene datos de hace menos de CONTACT_REFRESH_DAYS, se salta sin visitar el perfil.
CONTACT_REFRESH_DAYS      = max(1, int(os.getenv("CONTACT_REFRESH_DAYS", "30")))

COOLDOWN_FILE  = ".linkedin_429_cooldown"
LAST_RUN_FILE  = ".linkedin_last_run"


# ── Controles de seguridad ─────────────────────────────────────────────────────

def _check_cooldown() -> bool:
    """True = estamos en cooldown (no ejecutar)."""
    if not os.path.isfile(COOLDOWN_FILE):
        return False
    try:
        with open(COOLDOWN_FILE) as f:
            until = float(f.read().strip())
    except (ValueError, OSError):
        _remove_file(COOLDOWN_FILE)
        return False
    if time.time() < until:
        return True
    _remove_file(COOLDOWN_FILE)
    return False


def _write_cooldown() -> None:
    until = time.time() + COOLDOWN_HOURS_AFTER_429 * 3600
    try:
        with open(COOLDOWN_FILE, "w") as f:
            f.write(str(until))
    except OSError:
        pass


def _check_min_interval() -> bool:
    """True = no han pasado MIN_HOURS_BETWEEN_RUNS desde la última ejecución."""
    if MIN_HOURS_BETWEEN_RUNS <= 0:
        return False
    now = time.time()
    if os.path.isfile(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE) as f:
                last = float(f.read().strip())
            if now - last < MIN_HOURS_BETWEEN_RUNS * 3600:
                return True
        except (ValueError, OSError):
            pass
    try:
        with open(LAST_RUN_FILE, "w") as f:
            f.write(str(now))
    except OSError:
        pass
    return False


def _check_time_window() -> bool:
    """
    True = estamos FUERA de la franja horaria permitida y no debemos ejecutar.
    La franja va de SCRAPE_WINDOW_START a SCRAPE_WINDOW_END (hora local).
    """
    if SCRAPE_WINDOW_START == 0 and SCRAPE_WINDOW_END == 23:
        return False  # sin restricción horaria
    hour = datetime.now().hour
    if SCRAPE_WINDOW_START <= SCRAPE_WINDOW_END:
        return not (SCRAPE_WINDOW_START <= hour < SCRAPE_WINDOW_END)
    # Franja que cruza medianoche (ej. 22-6): es inusual pero soportado
    return not (hour >= SCRAPE_WINDOW_START or hour < SCRAPE_WINDOW_END)


def _check_daily_budget(username: str) -> bool:
    """True = ya se ha alcanzado el presupuesto diario (no ejecutar más)."""
    count = get_daily_count(username)
    return count >= MAX_CONTACTS_PER_DAY


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ── Helpers de usuario ────────────────────────────────────────────────────────

def extract_username(url: str) -> str:
    match = re.search(r"linkedin\.com/in/([^/?]+)", url)
    if not match:
        raise ValueError(f"URL inválida: {url}")
    return match.group(1).rstrip("/")


def get_username(account) -> str:
    username = get_current_username(account)
    if username:
        return username
    url = os.getenv("LINKEDIN_PROFILE_URL", "").strip()
    if url:
        return extract_username(url)
    print("No se pudo detectar tu usuario automáticamente.")
    url = input("🔗 Pega la URL de tu perfil de LinkedIn: ").strip()
    return extract_username(url)


def get_username_non_interactive(account) -> str:
    username = get_current_username(account)
    if username:
        return username
    url = os.getenv("LINKEDIN_PROFILE_URL", "").strip()
    if url:
        return extract_username(url)
    raise ValueError(
        "En modo no interactivo hace falta LINKEDIN_PROFILE_URL en .env "
        "o que la sesión devuelva el usuario."
    )


# ── Comprobaciones comunes ────────────────────────────────────────────────────

def _run_safety_checks(username: str, interactive: bool) -> None:
    """
    Lanza RuntimeError (modo no interactivo) o sys.exit(0) (interactivo)
    si algún control de seguridad impide ejecutar.
    """
    def _abort(msg: str) -> None:
        logger.warning(msg)
        print(f"⚠️  {msg}")
        if interactive:
            sys.exit(0)
        raise RuntimeError(msg)

    if _check_cooldown():
        try:
            with open(COOLDOWN_FILE) as f:
                until_dt = datetime.fromtimestamp(float(f.read())).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            until_dt = f"{COOLDOWN_HOURS_AFTER_429} h"
        _abort(f"Cooldown activo: LinkedIn bloqueó en una ejecución anterior. "
               f"No se ejecutará hasta: {until_dt}")

    if _check_min_interval():
        _abort(f"Solo se permite una ejecución cada {MIN_HOURS_BETWEEN_RUNS} h.")

    if _check_time_window():
        now_h = datetime.now().hour
        _abort(f"Fuera de la franja horaria permitida ({SCRAPE_WINDOW_START}:00–"
               f"{SCRAPE_WINDOW_END}:00). Hora actual: {now_h}:xx.")

    if username and _check_daily_budget(username):
        _abort(f"Presupuesto diario alcanzado: ya se han procesado "
               f"{get_daily_count(username)}/{MAX_CONTACTS_PER_DAY} "
               f"contactos hoy para '{username}'.")


# ── Modo INDEX ────────────────────────────────────────────────────────────────

def run_index(interactive: bool = True, account: Optional[str] = None) -> None:
    """
    Fase A: recopila todos los slugs de conexiones y los encola en contact_queue.
    No visita perfiles individuales → rápido y de bajo riesgo.

    account: slug de LinkedIn de la cuenta a usar (None = cuenta por defecto).
    """
    setup_logging()
    logger.info("run_index iniciado%s", f" [{account}]" if account else "")
    print("🗂️  Modo INDEX: recopilando índice de conexiones...\n")

    _run_safety_checks(username="", interactive=interactive)

    proxy = get_account_proxy(account) if account else None
    try:
        session = init_client(account=account, proxy=proxy)
    except RuntimeError:
        notify_session_expired(account)
        raise

    username = get_username(session) if interactive else get_username_non_interactive(session)
    logger.info("run_index: usuario=%s, proxy=%s", username, bool(proxy))

    slugs = collect_all_slugs(session, proxy=proxy)

    if not slugs:
        print("ℹ️  No se encontraron slugs. Revisa la sesión.")
        logger.warning("run_index: 0 slugs recopilados")
        return

    nuevos = queue_slugs(username, slugs)
    stats = get_queue_stats(username)
    logger.info("run_index: %d slugs totales, %d nuevos encolados", len(slugs), nuevos)
    notify_index_complete(account or username, len(slugs), nuevos)
    print(f"✅ Índice actualizado: {len(slugs)} conexiones encontradas, "
          f"{nuevos} nuevas encoladas.")
    print(f"   Cola actual → pending: {stats['pending']}, "
          f"done: {stats['done']}, error: {stats['error']}, "
          f"total: {stats['total']}")


# ── Modo ENRICH ───────────────────────────────────────────────────────────────

def run_enrich(
    interactive: bool = True,
    max_contacts_override: int | None = None,
    account: Optional[str] = None,
) -> None:
    """
    Fase B: toma slugs 'pending' de la cola y visita cada perfil para extraer
    datos completos. Guarda los resultados en la tabla contacts y marca cada
    slug como 'done' o 'error' en la queue.

    Lógica de skip inteligente: si un contacto ya existe y fue scrapeado hace
    menos de CONTACT_REFRESH_DAYS días, se marca done sin visitarlo (ahorra
    peticiones y reduce el riesgo de bloqueo).

    account: slug de LinkedIn de la cuenta a usar (None = cuenta por defecto).
    """
    setup_logging()
    logger.info("run_enrich iniciado%s", f" [{account}]" if account else "")
    print("👥 Modo ENRICH: enriqueciendo contactos pendientes...\n")

    proxy = get_account_proxy(account) if account else None
    try:
        session = init_client(account=account, proxy=proxy)
    except RuntimeError:
        notify_session_expired(account)
        raise

    username = get_username(session) if interactive else get_username_non_interactive(session)
    logger.info("run_enrich: usuario=%s, proxy=%s, refresh_days=%d", username, bool(proxy), CONTACT_REFRESH_DAYS)

    _run_safety_checks(username=username, interactive=interactive)

    # Recuperar más slugs de los que finalmente visitaremos; algunos se saltarán
    # por el skip inteligente, así que pedimos el doble para aprovechar el presupuesto.
    daily_used = get_daily_count(username)
    remaining_budget = max(0, MAX_CONTACTS_PER_DAY - daily_used)
    fetch_limit = min(
        (max_contacts_override or MAX_CONTACTS_PER_RUN) * 2,
        remaining_budget * 2,
        200,  # nunca pedir más de 200 a la vez
    )

    if remaining_budget <= 0:
        print(f"ℹ️  Presupuesto diario agotado ({MAX_CONTACTS_PER_DAY} contactos/día).")
        return

    slugs = get_pending_slugs(username, limit=fetch_limit)
    if not slugs:
        stats = get_queue_stats(username)
        print(f"ℹ️  No hay contactos pendientes en la cola. "
              f"(done: {stats['done']}, total: {stats['total']})")
        print("   Ejecuta '--mode index' para reindexar las conexiones.")
        return

    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    new_count = updated_count = skipped_count = error_count = visited = 0
    run_limit = max_contacts_override or MAX_CONTACTS_PER_RUN

    driver = _create_driver_with_cookies(session, proxy=proxy)
    if not driver:
        logger.error("run_enrich: no se pudo crear el WebDriver")
        print("❌ No se pudo abrir el navegador. Revisa la sesión.")
        return

    import random

    try:
        for slug in slugs:
            # Parar si ya alcanzamos el límite de visitas reales de esta ejecución
            # o si agotamos el presupuesto diario
            if visited >= run_limit or visited >= remaining_budget:
                break

            # ── Skip inteligente ──────────────────────────────────────────────
            if contact_exists(username, slug):
                days = days_since_last_scrape(username, slug)
                if days is not None and days < CONTACT_REFRESH_DAYS:
                    mark_queue_done(username, slug)
                    skipped_count += 1
                    logger.debug("skip %s (hace %.1f días)", slug, days)
                    continue  # no cuenta contra el presupuesto ni visita el perfil

            # ── Visita real del perfil ────────────────────────────────────────
            print(f"   [{visited + 1}/{run_limit}] {slug}", end="\r", flush=True)
            try:
                data = _enrich_connection_from_profile(driver, slug)
                result = upsert_contact(username, data)
                mark_queue_done(username, slug)
                visited += 1
                if result == "inserted":
                    new_count += 1
                else:
                    updated_count += 1
            except Exception as exc:
                logger.warning("run_enrich: error en %s: %s", slug, exc)
                mark_queue_error(username, slug, str(exc))
                error_count += 1
                visited += 1  # también cuenta: se hizo una petición

            # Pausa anti-detección (no pausar tras el último)
            if visited < run_limit and visited < remaining_budget:
                pause = random.uniform(4.0, 9.0)
                logger.debug("Pausa %.1fs antes del siguiente perfil", pause)
                time.sleep(pause)

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    print()  # nueva línea tras el \r
    finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    total_scraped = new_count + updated_count
    insert_run(
        username=username,
        started_at=started_at,
        finished_at=finished_at,
        contacts_scraped=total_scraped,
        contacts_new=new_count,
        contacts_updated=updated_count,
    )
    update_account_last_run(username)
    stats = get_queue_stats(username)
    logger.info(
        "run_enrich finalizado: new=%d updated=%d skipped=%d error=%d",
        new_count, updated_count, skipped_count, error_count,
    )
    notify_daily_summary(
        account=account or username,
        new_count=new_count,
        updated_count=updated_count,
        skipped_count=skipped_count,
        error_count=error_count,
        queue_pending=stats.get("pending", 0),
    )
    print(f"✅ Enriquecimiento completado: {new_count} nuevos, "
          f"{updated_count} actualizados, {skipped_count} saltados (frescos), "
          f"{error_count} errores.")
    print(f"   Cola → pending: {stats['pending']}, done: {stats['done']}, "
          f"error: {stats['error']}, total: {stats['total']}")

    on_block = getattr(session, "on_block", False)
    if on_block:
        _write_cooldown()
        notify_block(account=account or username, cooldown_hours=COOLDOWN_HOURS_AFTER_429)
        print(f"\n⚠️  LinkedIn limitó la sesión. Cooldown de "
              f"{COOLDOWN_HOURS_AFTER_429} h activado.")


# ── Modo LEGACY (compatibilidad con el flujo original) ────────────────────────

def run_scrape(
    interactive: bool = True,
    dry_run: bool = False,
    max_contacts_override: int | None = None,
    account: Optional[str] = None,
) -> None:
    """
    Flujo original (perfil + conexiones como CSV).
    Se mantiene para compatibilidad con invocaciones directas y tests existentes.

    account: slug de LinkedIn de la cuenta a usar (None = cuenta por defecto).
    """
    setup_logging()
    if not interactive:
        print("Scraping de conexiones (modo no interactivo)\n")
        logger.info("run_scrape iniciado (modo no interactivo)")
    else:
        print("Scraping de conexiones de tu cuenta (perfil + contactos)\n")
        logger.info("run_scrape iniciado (modo interactivo)")

    if _check_cooldown():
        try:
            with open(COOLDOWN_FILE) as f:
                until_dt = datetime.fromtimestamp(float(f.read())).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            until_dt = f"{COOLDOWN_HOURS_AFTER_429} h"
        msg = f"Cooldown activo hasta: {until_dt}"
        logger.warning(msg)
        print(f"⚠️  {msg}")
        if interactive:
            sys.exit(0)
        raise RuntimeError(msg)

    if _check_min_interval():
        msg = f"Solo se permite una ejecución cada {MIN_HOURS_BETWEEN_RUNS} h."
        logger.warning(msg)
        print(f"⚠️  {msg}")
        if interactive:
            sys.exit(0)
        raise RuntimeError(msg)

    if dry_run:
        logger.info("Dry-run: cooldown e intervalo OK")
        print("✅ Dry-run: cooldown e intervalo OK. No se ha ejecutado el scrape.")
        return

    proxy = get_account_proxy(account) if account else None
    try:
        session = init_client(account=account, proxy=proxy)
    except RuntimeError:
        notify_session_expired(account)
        raise
    username = get_username(session) if interactive else get_username_non_interactive(session)
    max_contacts = max(1, max_contacts_override) if max_contacts_override is not None else MAX_CONTACTS_PER_RUN
    logger.info("Usuario: %s, max_contacts: %s", username, max_contacts)

    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    os.makedirs("output", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")

    perfil, conexiones = scrape_profile_and_connections(session, username, max_contacts)

    finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    contacts_count = len(conexiones) if not conexiones.empty else 0
    on_block = getattr(session, "on_block", False)
    if conexiones.empty and not on_block:
        logger.warning("0 conexiones sin bloqueo. Revisar sesión.")

    insert_run(
        username=username,
        started_at=started_at,
        finished_at=finished_at,
        contacts_scraped=contacts_count,
        contacts_new=0,
        contacts_updated=0,
    )

    df_perfil = pd.DataFrame([perfil])
    f_perfil = f"output/perfil_{username}_{timestamp}.csv"
    df_perfil.to_csv(f_perfil, index=False, encoding="utf-8-sig")
    print(f"\n✅ Perfil guardado en: {f_perfil}")

    if not conexiones.empty:
        f_conexiones = f"output/conexiones_{username}_{timestamp}.csv"
        conexiones.to_csv(f_conexiones, index=False, encoding="utf-8-sig")
        print(f"✅ {len(conexiones)} conexiones guardadas en: {f_conexiones}")
        cols = [c for c in ["name", "position", "company", "location", "emails"] if c in conexiones.columns]
        if cols:
            print(conexiones[cols].head(10))
    else:
        print("ℹ️  No se obtuvieron conexiones")

    if on_block:
        _write_cooldown()
        notify_block(account=account or username, cooldown_hours=COOLDOWN_HOURS_AFTER_429)
        print(f"\n⚠️  LinkedIn limitó la sesión. Cooldown de {COOLDOWN_HOURS_AFTER_429} h activado.")

    update_account_last_run(username)
    logger.info("run_scrape finalizado: %d conexiones, on_block=%s", contacts_count, on_block)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scraper de conexiones de LinkedIn."
    )
    parser.add_argument(
        "--mode",
        choices=["index", "enrich", "legacy"],
        default="legacy",
        help=(
            "index  → recopilar slugs de conexiones en la cola (Fase A).\n"
            "enrich → enriquecer contactos pendientes con datos completos (Fase B).\n"
            "legacy → flujo original perfil+CSV (defecto)."
        ),
    )
    parser.add_argument(
        "--max-contacts", type=int, default=None, metavar="N",
        help="Límite de contactos por ejecución (sobrescribe MAX_CONTACTS_PER_RUN).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Solo comprobar controles de seguridad; no conectar ni scrapear.",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="No abrir navegador si la sesión caduca (útil en cron/servidor).",
    )
    parser.add_argument(
        "--account", type=str, default=None, metavar="SLUG",
        help=(
            "Cuenta LinkedIn a usar (slug, ej. 'miquel-roca-mascaros'). "
            "La sesión se cargará desde sessions/{slug}.pkl. "
            "Sin este argumento se usa la sesión por defecto (session.pkl)."
        ),
    )
    args = parser.parse_args()

    if args.no_browser:
        os.environ["LINKEDIN_NO_BROWSER"] = "1"
    interactive = not args.no_browser

    if args.dry_run:
        run_scrape(interactive=interactive, dry_run=True, account=args.account)
        return

    if args.mode == "index":
        run_index(interactive=interactive, account=args.account)
    elif args.mode == "enrich":
        run_enrich(
            interactive=interactive,
            max_contacts_override=args.max_contacts,
            account=args.account,
        )
    else:
        run_scrape(
            interactive=interactive,
            dry_run=False,
            max_contacts_override=args.max_contacts,
            account=args.account,
        )


if __name__ == "__main__":
    main()
