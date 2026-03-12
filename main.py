# main.py
# Solo modo 1: scraping de las conexiones de la cuenta que inicia sesión.

import os
import re
import sys
import time
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from scraper import (
    init_client,
    get_current_username,
    scrape_profile_and_connections,
)

load_dotenv()

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
    print(f"ℹ️  MAX_CONTACTS limitado a {MAX_CONTACTS} (solicitado: {_requested}, máximo: {_max_cap})")


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


def main():
    print("Scraping de conexiones de tu cuenta (perfil + contactos)\n")

    # No hacer ninguna petición si seguimos en cooldown por un 429 anterior
    if _check_cooldown():
        try:
            with open(COOLDOWN_FILE) as f:
                until = float(f.read().strip())
            until_dt = datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            until_dt = "24-48 h"
        print("⚠️  LinkedIn devolvió 429 en una ejecución anterior.")
        print(f"   No se hará ninguna petición hasta después de: {until_dt}")
        print("   (Puedes cambiar COOLDOWN_HOURS_AFTER_429 en .env o borrar el archivo .linkedin_429_cooldown para ignorar.)")
        sys.exit(0)

    # Opcional: no ejecutar más de una vez cada X horas (para pruebas sin saturar)
    if _check_min_interval():
        try:
            with open(LAST_RUN_FILE) as f:
                last = float(f.read().strip())
            next_ok = last + MIN_HOURS_BETWEEN_RUNS * 3600
            next_dt = datetime.fromtimestamp(next_ok).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            next_dt = f"en {MIN_HOURS_BETWEEN_RUNS} horas"
        print(f"⚠️  Solo se permite una ejecución cada {MIN_HOURS_BETWEEN_RUNS} h (MIN_HOURS_BETWEEN_RUNS).")
        print(f"   Próxima ejecución permitida: {next_dt}")
        print("   (Pon MIN_HOURS_BETWEEN_RUNS=0 en .env para desactivar este límite.)")
        sys.exit(0)

    account = init_client()
    username = get_username(account)

    os.makedirs("output", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")

    perfil, conexiones = scrape_profile_and_connections(account, username, MAX_CONTACTS)

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

    # Si LinkedIn devolvió 429 o TooManyRedirects: guardar estado y no volver a hacer peticiones hasta el cooldown
    if getattr(account, "on_block", False):
        _write_cooldown()
        print("")
        print("⚠️  LinkedIn ha limitado la sesión (429 o redirecciones en bucle).")
        print("   Se ha guardado el estado: en la próxima ejecución no se hará ninguna petición")
        print(f"   hasta que pasen {COOLDOWN_HOURS_AFTER_429} horas (configurable con COOLDOWN_HOURS_AFTER_429 en .env).")
        print("   Para reducir más el riesgo, usa MAX_CONTACTS más bajo o sube SLEEP_BETWEEN_CONNECTIONS.")
        print("")


if __name__ == "__main__":
    main()
