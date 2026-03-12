# main.py
# Solo modo 1: scraping de las conexiones de la cuenta que inicia sesión.

import argparse
import logging
import os
import re
import sys
import time
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv
from scraper import (
    init_client,
    get_current_username,
    scrape_profile_and_connections,
)
from db import insert_run
from log_config import setup_logging

load_dotenv()

logger = logging.getLogger(__name__)

# Cooldown tras 429: no hacer más peticiones hasta que pasen estas horas
COOLDOWN_HOURS_AFTER_429 = int(os.getenv("COOLDOWN_HOURS_AFTER_429", "48"))
COOLDOWN_FILE = ".linkedin_429_cooldown"

# Mínimo de horas entre ejecuciones (0 = desactivado). Útil en pruebas para no saturar.
MIN_HOURS_BETWEEN_RUNS = int(os.getenv("MIN_HOURS_BETWEEN_RUNS", "0"))
LAST_RUN_FILE = ".linkedin_last_run"

# Límite de conexiones a scrapear; cap para no saturar y evitar 429/bloqueos
_default_max = 15
_max_cap = int(os.getenv("MAX_CONTACTS_CAP", "50"))
_raw = os.getenv("MAX_CONTACTS", str(_default_max))
try:
    _requested = int(_raw)
except (TypeError, ValueError):
    _requested = _default_max
MAX_CONTACTS = max(1, min(_requested, _max_cap))
if _requested != MAX_CONTACTS:
    msg = f"MAX_CONTACTS limitado a {MAX_CONTACTS} (solicitado: {_requested}, máximo: {_max_cap})"
    logger.info(msg)
    print(f"ℹ️  {msg}")


def _check_cooldown() -> bool:
    """
    True = estamos en cooldown, no ejecutar (ni siquiera conectar).
    False = podemos ejecutar. Si el archivo existía y ya pasó el tiempo, lo borra.
    """
    if not os.path.isfile(COOLDOWN_FILE):
        return False
    try:
        with open(COOLDOWN_FILE) as f:
            until = float(f.read().strip())
    except (ValueError, OSError):
        try:
            os.remove(COOLDOWN_FILE)
        except OSError:
            pass
        return False
    if time.time() < until:
        return True
    try:
        os.remove(COOLDOWN_FILE)
    except OSError:
        pass
    return False


def _write_cooldown() -> None:
    """Guarda estado: no volver a hacer peticiones hasta pasadas COOLDOWN_HOURS_AFTER_429."""
    until = time.time() + COOLDOWN_HOURS_AFTER_429 * 3600
    try:
        with open(COOLDOWN_FILE, "w") as f:
            f.write(str(until))
    except OSError:
        pass


def _check_min_interval() -> bool:
    """
    True = ha pasado menos de MIN_HOURS_BETWEEN_RUNS desde la última ejecución, no arrancar.
    False = podemos ejecutar. Si pasamos, guardamos timestamp de esta ejecución.
    """
    if MIN_HOURS_BETWEEN_RUNS <= 0:
        return False
    now = time.time()
    if os.path.isfile(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE) as f:
                last = float(f.read().strip())
        except (ValueError, OSError):
            last = 0
        if now - last < MIN_HOURS_BETWEEN_RUNS * 3600:
            return True
    try:
        with open(LAST_RUN_FILE, "w") as f:
            f.write(str(now))
    except OSError:
        pass
    return False


def extract_username(url: str) -> str:
    match = re.search(r"linkedin\.com/in/([^/?]+)", url)
    if not match:
        raise ValueError(f"URL inválida: {url}")
    return match.group(1).rstrip("/")


def get_username(account) -> str:
    """
    Usuario para los archivos: quien ha iniciado sesión (detectado por API)
    o LINKEDIN_PROFILE_URL en .env; si no, se pide la URL como fallback.
    """
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
    """
    Usuario en modo no interactivo (sin input): API o LINKEDIN_PROFILE_URL.
    Lanza ValueError si no se puede obtener (para cron/viewer).
    """
    username = get_current_username(account)
    if username:
        return username
    url = os.getenv("LINKEDIN_PROFILE_URL", "").strip()
    if url:
        return extract_username(url)
    raise ValueError(
        "En modo no interactivo hace falta LINKEDIN_PROFILE_URL en .env o que la sesión devuelva el usuario."
    )


def run_scrape(
    interactive: bool = True,
    dry_run: bool = False,
    max_contacts_override: int | None = None,
) -> None:
    """
    Ejecuta el scraping de conexiones (perfil + contactos).
    - interactive=True: como main(), puede pedir URL por teclado si no hay usuario.
    - interactive=False: no pide input; usa usuario de la API o LINKEDIN_PROFILE_URL (sino, lanza).
    - dry_run=True: solo comprueba cooldown e intervalo mínimo; no conecta ni scrapea.
    - max_contacts_override: si no es None, usa este límite en lugar de MAX_CONTACTS.
    Registra cada ejecución en la tabla runs para el viewer.
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
                until = float(f.read().strip())
            until_dt = datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            until_dt = "24-48 h"
        msg = (
            f"LinkedIn devolvió 429 en una ejecución anterior. No se hará ninguna petición hasta después de: {until_dt}"
        )
        logger.warning("Cooldown activo: %s", until_dt)
        print(f"⚠️  {msg}")
        if interactive:
            sys.exit(0)
        raise RuntimeError(msg)

    if _check_min_interval():
        try:
            with open(LAST_RUN_FILE) as f:
                last = float(f.read().strip())
            next_ok = last + MIN_HOURS_BETWEEN_RUNS * 3600
            next_dt = datetime.fromtimestamp(next_ok).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            next_dt = f"en {MIN_HOURS_BETWEEN_RUNS} horas"
        msg = f"Solo se permite una ejecución cada {MIN_HOURS_BETWEEN_RUNS} h. Próxima permitida: {next_dt}"
        logger.warning("Intervalo mínimo no cumplido: %s", next_dt)
        print(f"⚠️  {msg}")
        if interactive:
            sys.exit(0)
        raise RuntimeError(msg)

    if dry_run:
        logger.info("Dry-run: cooldown e intervalo OK, no se ejecuta el scrape")
        print("✅ Dry-run: cooldown e intervalo OK. No se ha ejecutado el scrape.")
        return

    account = init_client()
    username = get_username(account) if interactive else get_username_non_interactive(account)
    max_contacts = max(1, max_contacts_override) if max_contacts_override is not None else MAX_CONTACTS
    logger.info("Usuario: %s, max_contacts: %s", username, max_contacts)

    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    os.makedirs("output", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")

    perfil, conexiones = scrape_profile_and_connections(account, username, max_contacts)

    finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    contacts_count = len(conexiones) if not conexiones.empty else 0
    on_block = getattr(account, "on_block", False)
    if conexiones.empty and not on_block:
        logger.warning(
            "Ejecución sospechosa: 0 conexiones sin 429/redirects. Revisar sesión o cambios en LinkedIn."
        )
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
        if not on_block:
            print("   (Si esperabas conexiones, revisa la sesión o si LinkedIn ha cambiado.)")

    if on_block:
        _write_cooldown()
        print("")
        print("⚠️  LinkedIn ha limitado la sesión (429 o redirecciones en bucle).")
        print("   Se ha guardado el estado: en la próxima ejecución no se hará ninguna petición")
        print(f"   hasta que pasen {COOLDOWN_HOURS_AFTER_429} horas (configurable con COOLDOWN_HOURS_AFTER_429 en .env).")
        print("   Para reducir más el riesgo, usa MAX_CONTACTS más bajo o sube SLEEP_BETWEEN_CONNECTIONS.")
        print("")
    logger.info("run_scrape finalizado: %s conexiones, on_block=%s", contacts_count, on_block)


def main():
    parser = argparse.ArgumentParser(
        description="Scraping de conexiones de LinkedIn (perfil + contactos)."
    )
    parser.add_argument(
        "--max-contacts",
        type=int,
        default=None,
        metavar="N",
        help="Límite de conexiones a scrapear (sobrescribe MAX_CONTACTS/.env).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo comprobar cooldown e intervalo mínimo; no conectar ni scrapear.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="No abrir navegador si la sesión ha caducado; fallar y avisar (útil en cron).",
    )
    args = parser.parse_args()

    if args.no_browser:
        os.environ["LINKEDIN_NO_BROWSER"] = "1"
    interactive = not args.no_browser
    run_scrape(
        interactive=interactive,
        dry_run=args.dry_run,
        max_contacts_override=args.max_contacts,
    )


if __name__ == "__main__":
    main()
