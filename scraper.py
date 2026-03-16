# scraper.py
# Scraping de LinkedIn usando Selenium puro (sin StaffSpy).
# El login se gestiona con cookies persistidas en session.pkl.

import json
import logging
import os
import pickle
import random
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

load_dotenv()

_log = logging.getLogger(__name__)


# ── Configuración ──────────────────────────────────────────────────────────────

def _get_env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _get_env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


BROWSER_PROFILE_WAIT      = _get_env_int("BROWSER_PROFILE_WAIT", 10)
SLEEP_BETWEEN_CONNECTIONS = _get_env_float("SLEEP_BETWEEN_CONNECTIONS", 6.0)
MAX_CONTACTS_CAP          = _get_env_int("MAX_CONTACTS_CAP", 50)
SESSION_FILE              = "session.pkl"
SESSIONS_DIR              = os.getenv("SESSIONS_DIR", "sessions")
# Muestra el navegador si HEADLESS=false en .env (útil para depuración)
HEADLESS                  = os.getenv("HEADLESS", "true").lower() != "false"

# LinkedIn redirige invite-manager/ a catch-up/ — usamos la URL real
_CONNECTIONS_URL        = "https://www.linkedin.com/mynetwork/catch-up/connections/"
_CONNECTIONS_SEARCH_URL = "https://www.linkedin.com/search/results/people/?network=%5B%22F%22%5D&origin=MEMBER_PROFILE_CANNED_SEARCH"


# ── Sesión ─────────────────────────────────────────────────────────────────────

class LinkedInSession:
    """Contenedor ligero de la sesión de LinkedIn (cookies + estado)."""

    def __init__(self, cookies: List[Dict], username: Optional[str] = None):
        # cookies: lista de dicts {name, value, domain, path}
        self._cookies: List[Dict] = cookies
        self.on_block: bool = False
        # Username detectado durante init_client (slug de LinkedIn, ej. "miquel-roca-mascaros")
        self.username: Optional[str] = username

    @property
    def cookies(self) -> List[Dict]:
        return self._cookies


def _load_cookies(path: str = SESSION_FILE) -> Optional[List[Dict]]:
    """
    Carga cookies desde session.pkl.
    Soporta el formato antiguo (requests.cookies.RequestsCookieJar)
    y el nuevo (lista de dicts).
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
        cookies = data.get("cookies", [])
        # Formato antiguo: RequestsCookieJar (iterable de objetos con .name, .value…)
        if not isinstance(cookies, list):
            converted = []
            for c in cookies:
                converted.append({
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain or ".linkedin.com",
                    "path": c.path or "/",
                })
            return converted
        return cookies
    except Exception as e:
        _log.warning("No se pudo cargar %s: %s", path, e)
        return None


def _save_cookies(cookies: List[Dict], path: str = SESSION_FILE) -> None:
    """Guarda la lista de cookies en session.pkl."""
    try:
        with open(path, "wb") as f:
            pickle.dump({"cookies": cookies}, f)
        _log.info("Cookies guardadas en %s", path)
    except Exception as e:
        _log.warning("No se pudo guardar cookies en %s: %s", path, e)


def _driver_cookies_to_list(driver) -> List[Dict]:
    """Extrae todas las cookies del driver como lista de dicts."""
    return [
        {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ".linkedin.com"),
            "path": c.get("path", "/"),
        }
        for c in driver.get_cookies()
    ]


# ── WebDriver ──────────────────────────────────────────────────────────────────

# User-Agent de un Chrome reciente en macOS (actualizar si la versión queda obsoleta)
_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)

# Script que se inyecta en cada nueva página para ocultar que es un navegador automatizado.
# Cubre las comprobaciones más habituales de LinkedIn y otras webs anti-bot.
_STEALTH_SCRIPT = """
    // Ocultar navigator.webdriver (señal primaria de Selenium/WebDriver)
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

    // Simular plugins de un navegador real (headless los tiene vacíos)
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            {name: 'Chrome PDF Plugin'},
            {name: 'Chrome PDF Viewer'},
            {name: 'Native Client'}
        ]
    });

    // Idiomas típicos de un usuario de habla hispana en macOS
    Object.defineProperty(navigator, 'languages', {
        get: () => ['es-ES', 'es', 'en-US', 'en']
    });

    // window.chrome existe en Chrome real pero no en headless por defecto
    if (!window.chrome) {
        window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    }

    // Ocultar el flag "HeadlessChrome" del user-agent que Chrome inyecta internamente
    const origUA = navigator.userAgent;
    Object.defineProperty(navigator, 'userAgent', {
        get: () => origUA.replace('HeadlessChrome', 'Chrome')
    });
"""


def _parse_proxy(proxy_str: str) -> dict:
    """
    Parsea un proxy en cualquiera de estos formatos:
      - host:port
      - http://host:port
      - http://user:pass@host:port
      - user:pass@host:port

    Devuelve un dict con claves: host, port, user (opcional), password (opcional).
    """
    s = proxy_str.strip()
    if s.startswith("http://") or s.startswith("https://"):
        s = s.split("://", 1)[1]
    user = password = None
    if "@" in s:
        credentials, hostport = s.rsplit("@", 1)
        if ":" in credentials:
            user, password = credentials.split(":", 1)
        else:
            user = credentials
    else:
        hostport = s
    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
    else:
        host, port = hostport, "8080"
    return {"host": host, "port": port, "user": user, "password": password}


def _create_proxy_auth_extension(host: str, port: str, user: str, password: str) -> str:
    """
    Crea una extensión de Chrome temporal (.zip) para proxies con autenticación.
    Chrome no admite credenciales en --proxy-server, pero sí a través de una
    extensión que intercepta el evento onAuthRequired.
    Devuelve la ruta al archivo .zip creado en el directorio temporal del sistema.
    """
    import tempfile
    import zipfile

    manifest = """{
  "version": "1.0.0",
  "manifest_version": 2,
  "name": "Scraper Proxy Auth",
  "permissions": [
    "proxy", "tabs", "unlimitedStorage", "storage",
    "<all_urls>", "webRequest", "webRequestBlocking"
  ],
  "background": {"scripts": ["background.js"]},
  "minimum_chrome_version": "22.0.0"
}"""

    background = f"""var config = {{
  mode: "fixed_servers",
  rules: {{
    singleProxy: {{ scheme: "http", host: "{host}", port: parseInt("{port}") }},
    bypassList: ["localhost", "127.0.0.1"]
  }}
}};
chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});
chrome.webRequest.onAuthRequired.addListener(
  function(details) {{
    return {{ authCredentials: {{ username: "{user}", password: "{password}" }} }};
  }},
  {{urls: ["<all_urls>"]}},
  ["blocking"]
);"""

    ext_path = os.path.join(tempfile.gettempdir(), f"scraper_proxy_{host}_{port}.zip")
    with zipfile.ZipFile(ext_path, "w") as zf:
        zf.writestr("manifest.json", manifest)
        zf.writestr("background.js", background)
    return ext_path


def _make_chrome_options(headless: bool = True, proxy: Optional[str] = None) -> ChromeOptions:
    """
    Construye las opciones de Chrome con soporte opcional de proxy.

    El binario de Chrome se puede sobreescribir con la variable de entorno
    CHROME_BINARY. Útil en servidores ARM (Oracle Cloud) donde el binario
    es 'chromium-browser' en lugar de 'google-chrome'.

    proxy formato:
      - 'host:port'              → proxy sin autenticación
      - 'user:pass@host:port'    → proxy con autenticación (genera extensión temporal)
      - None                     → sin proxy
    """
    opts = ChromeOptions()

    # Binario personalizado (necesario en ARM con chromium-browser)
    chrome_binary = os.environ.get("CHROME_BINARY", "").strip()
    if chrome_binary:
        opts.binary_location = chrome_binary

    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1280,800")
    opts.add_argument(f"--user-agent={_CHROME_UA}")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    # Flags para reducir consumo de memoria en servidores con RAM limitada (≤1 GB)
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-plugins")
    opts.add_argument("--disable-images")
    opts.add_argument("--disable-javascript-harmony-shipping")
    opts.add_argument("--no-zygote")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-sync")
    opts.add_argument("--disable-translate")
    opts.add_argument("--hide-scrollbars")
    opts.add_argument("--mute-audio")
    opts.add_argument("--safebrowsing-disable-auto-update")
    opts.add_argument("--js-flags=--max-old-space-size=256")

    if proxy:
        try:
            p = _parse_proxy(proxy)
            if p["user"] and p["password"]:
                # Proxy con auth: necesita extensión (Chrome no admite user:pass en --proxy-server)
                ext_path = _create_proxy_auth_extension(
                    p["host"], p["port"], p["user"], p["password"]
                )
                opts.add_extension(ext_path)
                _log.info("Proxy con auth configurado via extensión: %s:%s", p["host"], p["port"])
            else:
                # Proxy sin auth: --proxy-server es suficiente
                opts.add_argument(f"--proxy-server=http://{p['host']}:{p['port']}")
                _log.info("Proxy sin auth configurado: %s:%s", p["host"], p["port"])
        except Exception as e:
            _log.warning("No se pudo configurar el proxy '%s': %s — continuando sin proxy", proxy, e)

    return opts


def _apply_stealth(driver) -> None:
    """
    Inyecta el script anti-detección en cada nueva página que cargue el driver.
    Debe llamarse justo después de crear el driver y antes de la primera navegación.
    """
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": _STEALTH_SCRIPT
        })
    except Exception as e:
        _log.warning("No se pudo aplicar el script stealth (CDP): %s", e)


def _detect_username_from_driver(driver) -> Optional[str]:
    """
    Intenta extraer el username (slug) del usuario logueado usando el driver activo.
    Navega a /in/me (LinkedIn redirige al perfil real) y extrae el slug de la URL.
    También intenta extraerlo del HTML del feed como fallback.
    """
    try:
        driver.get("https://www.linkedin.com/in/me")
        time.sleep(random.uniform(2.5, 4.0))
        url = driver.current_url
        m = re.search(r"linkedin\.com/in/([^/?#]+)", url)
        if m:
            slug = m.group(1).rstrip("/").lower()
            if slug and slug not in ("me", "login", "feed") and len(slug) > 2:
                _log.info("Username detectado via /in/me: %s", slug)
                return slug
    except Exception as e:
        _log.debug("_detect_username_from_driver (/in/me) falló: %s", e)

    # Fallback: buscar el enlace al propio perfil en el HTML actual
    try:
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(random.uniform(2.0, 3.0))
        html = driver.page_source
        # LinkedIn inyecta el perfil del usuario en el HTML del feed
        for pat in [
            r'"publicIdentifier"\s*:\s*"([a-z0-9][a-z0-9\-]{2,})"',
            r'linkedin\.com/in/([a-z0-9][a-z0-9\-]{2,})(?:/|")',
        ]:
            m = re.search(pat, html)
            if m:
                slug = m.group(1).rstrip("/").lower()
                if slug and len(slug) > 2:
                    _log.info("Username detectado via HTML del feed: %s", slug)
                    return slug
    except Exception as e:
        _log.debug("_detect_username_from_driver (feed fallback) falló: %s", e)

    return None


def _is_logged_in(driver) -> bool:
    """Comprueba si el driver actual está autenticado en LinkedIn."""
    url = driver.current_url
    return not any(kw in url for kw in ("authwall", "/login", "checkpoint", "uas/login", "signup"))


def _is_soft_blocked(driver) -> bool:
    """
    Detecta bloqueos suaves que NO cambian la URL:
    páginas de error, captcha, verificación de seguridad, rate-limit.
    Devuelve True si se detecta alguno.
    """
    try:
        title = (driver.title or "").lower()
        if any(kw in title for kw in ("security verification", "captcha", "verification")):
            return True
        body_els = driver.find_elements(By.CSS_SELECTOR, "body")
        if not body_els:
            return False
        text = body_els[0].text.lower()
        soft_block_phrases = [
            "something went wrong",
            "algo salió mal",
            "too many redirects",
            "this page is unavailable",
            "we couldn't load this page",
            "please verify you are a human",
            "security check",
            "unusual activity",
        ]
        return any(phrase in text for phrase in soft_block_phrases)
    except Exception:
        return False


def _inject_cookies(driver, cookies: List[Dict]) -> None:
    """
    Inyecta cookies en el driver.
    Requiere estar ya en linkedin.com antes de llamar a esta función.
    """
    for c in cookies:
        domain = c.get("domain") or ".linkedin.com"
        if not domain.startswith(".") and "linkedin" in domain:
            domain = "." + domain
        try:
            driver.add_cookie({
                "name": c["name"],
                "value": c["value"],
                "domain": domain,
                "path": c.get("path", "/"),
            })
        except Exception:
            pass


# ── Login / init_client ────────────────────────────────────────────────────────

def _is_interactive() -> bool:
    """True si el proceso tiene terminal (puede mostrar un navegador al usuario)."""
    return sys.stdin.isatty()


def session_file_for(account: Optional[str] = None) -> str:
    """
    Devuelve la ruta al archivo de sesión para la cuenta indicada.
    - Sin cuenta: usa session.pkl (comportamiento original).
    - Con cuenta: usa sessions/{account}.pkl creando el directorio si hace falta.
    """
    if not account:
        return SESSION_FILE
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", account)
    return os.path.join(SESSIONS_DIR, f"{safe}.pkl")


def init_client(account: Optional[str] = None, proxy: Optional[str] = None) -> LinkedInSession:
    """
    Inicializa la sesión de LinkedIn usando Selenium puro.

    account: nombre de la cuenta (slug de LinkedIn). Si se indica, las cookies
             se guardan en sessions/{account}.pkl en lugar de session.pkl.
    proxy:   proxy para esta cuenta. Formato 'host:port' o 'user:pass@host:port'.
             Si es None, no se usa proxy.

    Flujo:
    1. Carga cookies del archivo de sesión correspondiente (si existen).
    2. Abre Chrome headless (con proxy si aplica), inyecta cookies y comprueba sesión.
    3. Si la sesión ha caducado (o no hay cookies):
       a. En modo interactivo: abre Chrome visible para que el usuario haga login.
       b. En modo no interactivo (cron / --no-browser): lanza RuntimeError.
    4. Guarda las cookies nuevas y devuelve la sesión.
    """
    session_path = session_file_for(account)
    account_label = f" [{account}]" if account else ""
    proxy_label = f" (proxy: {proxy.split('@')[-1] if proxy and '@' in proxy else proxy})" if proxy else ""
    print(f"🔐 Conectando a LinkedIn{account_label}{proxy_label}...")
    no_browser = os.environ.get("LINKEDIN_NO_BROWSER", "").strip() in ("1", "true", "yes")
    cookies = _load_cookies(session_path)

    # Paso 1: comprobar si las cookies guardadas siguen siendo válidas.
    # En servidores con poca RAM usamos una validación ligera (eager loading + timeout corto)
    # para evitar que Chrome se quede sin memoria cargando LinkedIn completo.
    if cookies:
        import stat as _stat

        # Optimización para RAM baja: si el pkl tiene menos de 6h de antigüedad lo
        # consideramos válido directamente sin abrir Chrome (LinkedIn no caduca tan rápido).
        pkl_age_hours = 999
        try:
            pkl_mtime = os.path.getmtime(session_path)
            pkl_age_hours = (time.time() - pkl_mtime) / 3600
        except Exception:
            pass

        if pkl_age_hours < 6:
            _log.info(
                "Sesión reciente (%.1fh). Usando cookies directamente sin validar con Chrome.",
                pkl_age_hours,
            )
            print(f"✅ Sesión activa (fichero reciente, {pkl_age_hours:.1f}h).")
            # Intentar extraer username del nombre del archivo
            username = account if account else None
            return LinkedInSession(cookies, username=username)

        driver = None
        try:
            opts = _make_chrome_options(headless=True, proxy=proxy)
            opts.page_load_strategy = "eager"   # no esperar CSS/imágenes, solo DOM
            driver = webdriver.Chrome(options=opts)
            driver.set_page_load_timeout(45)     # falla rápido en vez de esperar 120s
            _apply_stealth(driver)
            driver.get("https://www.linkedin.com")
            time.sleep(random.uniform(1.0, 1.5))
            _inject_cookies(driver, cookies)
            driver.get("https://www.linkedin.com/feed/")
            time.sleep(random.uniform(1.5, 2.5))
            if _is_logged_in(driver):
                username = _detect_username_from_driver(driver)
                if username:
                    _log.info("Username detectado durante init_client: %s", username)
                fresh_cookies = _driver_cookies_to_list(driver)
                _save_cookies(fresh_cookies, session_path)
                _log.info("Sesión válida cargada desde %s", session_path)
                print(f"✅ Sesión activa{f' ({username})' if username else ''}.")
                return LinkedInSession(fresh_cookies, username=username)
            else:
                _log.info("Cookies caducadas o sesión inválida, necesario re-login")
                print("ℹ️  La sesión guardada ha caducado. Necesitas volver a iniciar sesión.")
        except Exception as e:
            _log.warning("Error comprobando sesión existente: %s", e)
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
    else:
        print("ℹ️  No hay sesión guardada. Necesitas iniciar sesión en LinkedIn.")

    # Paso 2: login manual (modo interactivo)
    if not _is_interactive() or no_browser:
        if no_browser:
            _log.warning("Sesión inválida y LINKEDIN_NO_BROWSER=1: no se abre navegador.")
        msg = (
            "⚠️  No hay sesión válida y no se puede abrir el navegador "
            "(modo no interactivo o --no-browser).\n"
            "   Ejecuta el script manualmente para volver a iniciar sesión."
        )
        print(msg)
        raise RuntimeError(msg)

    print("   Abriendo Chrome para que inicies sesión en LinkedIn...")
    print("   (Completa el login, incluida cualquier verificación en dos pasos o captcha.)")
    driver = None
    try:
        driver = webdriver.Chrome(options=_make_chrome_options(headless=False, proxy=proxy))
        _apply_stealth(driver)
        driver.get("https://www.linkedin.com/login")
        input("\n   Pulsa Enter cuando hayas iniciado sesión y estés en LinkedIn (Feed o perfil)...\n")
        if not _is_logged_in(driver):
            print("⚠️  No parece que hayas completado el login. Vuelve a ejecutar el script.")
            raise RuntimeError("Login no completado")
        # Detectar username y recoger cookies mientras el driver está activo
        username = _detect_username_from_driver(driver)
        if username:
            _log.info("Username detectado tras login: %s", username)
        fresh_cookies = _driver_cookies_to_list(driver)
        _save_cookies(fresh_cookies, session_path)
        _log.info("Login completado, cookies guardadas en %s", session_path)
        print(f"✅ Login completado y sesión guardada{f' ({username})' if username else ''}.")
        return LinkedInSession(fresh_cookies, username=username)
    except RuntimeError:
        raise
    except Exception as e:
        print(
            "\n⚠️  El login no se completó. Si LinkedIn mostró captcha o verificación, "
            "complétala antes de pulsar Enter. Vuelve a ejecutar el script si hace falta."
        )
        raise RuntimeError(f"Error durante el login: {e}") from e
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ── Login automático con credenciales ─────────────────────────────────────────

def login_with_credentials(
    account: str,
    email: str,
    password: str,
    proxy: Optional[str] = None,
    headless: bool = False,
) -> dict:
    """
    Realiza el login automatizado en LinkedIn con email y contraseña.

    headless=False (por defecto) → Chrome visible. Recomendado para el primer
      login desde la vista, donde el usuario puede completar 2FA o captcha.
    headless=True → Chrome oculto. Usado en re-login automático desde el cron.
      Si LinkedIn pide verificación, se retorna "needs_verification" y se notifica
      por Telegram para que el usuario lo haga manualmente.

    Retorna un dict con una de estas claves "status":
      "ok"                 → sesión guardada en sessions/{account}.pkl
      "needs_verification" → LinkedIn pide 2FA / email-code / captcha
      "wrong_credentials"  → email o contraseña incorrectos
      "error"              → error inesperado (mensaje en "message")
    """
    from selenium.webdriver.common.by import By

    session_path = session_file_for(account)
    driver = None
    try:
        driver = webdriver.Chrome(options=_make_chrome_options(headless=headless, proxy=proxy))
        _apply_stealth(driver)
        driver.get("https://www.linkedin.com/login")
        time.sleep(random.uniform(2.0, 3.5))

        # Rellenar email tecla a tecla
        email_field = driver.find_element(By.ID, "username")
        email_field.clear()
        for char in email:
            email_field.send_keys(char)
            time.sleep(random.uniform(0.04, 0.11))
        time.sleep(random.uniform(0.4, 0.9))

        # Rellenar contraseña tecla a tecla
        pass_field = driver.find_element(By.ID, "password")
        pass_field.clear()
        for char in password:
            pass_field.send_keys(char)
            time.sleep(random.uniform(0.04, 0.10))
        time.sleep(random.uniform(0.4, 0.8))

        # Clic en "Sign in"
        driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()

        # Esperar redirección (puede tardar varios segundos)
        time.sleep(random.uniform(5.0, 7.0))
        current_url = driver.current_url

        # ── Sesión activa ──
        if _is_logged_in(driver):
            username = _detect_username_from_driver(driver)
            fresh_cookies = _driver_cookies_to_list(driver)
            _save_cookies(fresh_cookies, session_path)
            _log.info("Login automático OK para cuenta %s → %s", account, session_path)
            return {"status": "ok", "detected_username": username or account}

        # ── LinkedIn pide verificación adicional (2FA, código por email, captcha) ──
        verification_keywords = ("checkpoint", "verification", "challenge", "captcha", "pin")
        if any(kw in current_url.lower() for kw in verification_keywords):
            _log.warning("Login requiere verificación para %s: %s", account, current_url)
            return {
                "status": "needs_verification",
                "message": (
                    "LinkedIn requiere verificación adicional (código por email, "
                    "teléfono o captcha). Completa el proceso manualmente ejecutando "
                    f"'python main.py --account={account}' en el servidor."
                ),
            }

        # ── Credenciales incorrectas ──
        try:
            page_src = driver.page_source.lower()
        except Exception:
            page_src = ""
        if "/login" in current_url or "uas/login" in current_url:
            if any(kw in page_src for kw in ("incorrect", "wrong", "invalid", "error")):
                return {
                    "status": "wrong_credentials",
                    "message": "Email o contraseña incorrectos. Revisa los datos e inténtalo de nuevo.",
                }
            return {
                "status": "wrong_credentials",
                "message": "No se pudo iniciar sesión. Comprueba el email y la contraseña.",
            }

        return {
            "status": "error",
            "message": f"Estado desconocido tras el login. URL: {current_url}",
        }

    except Exception as exc:
        _log.error("Error en login_with_credentials para %s: %s", account, exc)
        return {"status": "error", "message": str(exc)}
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ── Username ───────────────────────────────────────────────────────────────────

def get_current_username(session: LinkedInSession) -> Optional[str]:
    """
    Devuelve el username detectado durante init_client si ya está en la sesión.
    Si no, abre un driver headless como fallback para intentar extraerlo.
    """
    # Camino rápido: username ya detectado durante init_client (sin abrir otro Chrome)
    if session.username:
        _log.info("Username disponible desde la sesión: %s", session.username)
        return session.username

    # Fallback: abrir un driver headless con las cookies e intentar detectarlo
    driver = None
    try:
        driver = webdriver.Chrome(options=_make_chrome_options(headless=True))
        _apply_stealth(driver)
        driver.get("https://www.linkedin.com")
        time.sleep(random.uniform(1.0, 2.0))
        _inject_cookies(driver, session.cookies)
        driver.refresh()
        time.sleep(random.uniform(1.5, 2.5))
        username = _detect_username_from_driver(driver)
        if username:
            session.username = username  # cachear para futuros accesos
        return username
    except Exception as e:
        _log.warning("get_current_username (fallback driver) falló: %s", e)
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ── Utilidades de parseo ───────────────────────────────────────────────────────

def _find_in_dict(obj, *keys: str) -> Optional[str]:
    """Busca la primera clave (de una lista) que exista en un dict con valor string."""
    if not isinstance(obj, dict):
        return None
    for key in keys:
        if key in obj:
            val = obj[key]
            if isinstance(val, str) and val:
                return val
    return None


def _deep_find_value(obj, target_key: str) -> Optional[str]:
    """Recorre recursivamente dict/list y devuelve el primer valor string para target_key."""
    if isinstance(obj, dict):
        if target_key in obj:
            v = obj[target_key]
            if isinstance(v, str) and v:
                return v
        for v in obj.values():
            found = _deep_find_value(v, target_key)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find_value(item, target_key)
            if found:
                return found
    return None


def _parse_person_from_json_ld(parsed: dict) -> Optional[Dict]:
    """Extrae campos de Person desde un bloque JSON-LD de LinkedIn."""
    person = None
    if isinstance(parsed, dict) and "@graph" in parsed:
        for item in parsed.get("@graph", []):
            if isinstance(item, dict) and item.get("@type") == "Person":
                person = item
                break
    elif isinstance(parsed, dict) and parsed.get("@type") == "Person":
        person = parsed
    if not person:
        return None

    addr = person.get("address") or {}
    if isinstance(addr, dict):
        loc_parts = [
            addr.get("addressLocality"),
            addr.get("addressRegion"),
            addr.get("addressCountry"),
        ]
        location = ", ".join(filter(None, loc_parts)) or None
    else:
        location = None

    works_for = person.get("worksFor")
    company = None
    if isinstance(works_for, list) and works_for:
        company = works_for[0].get("name") if isinstance(works_for[0], dict) else None
    elif isinstance(works_for, dict):
        company = works_for.get("name")

    # Foto de perfil (LinkedIn la incluye en JSON-LD como campo "image")
    image = person.get("image")
    profile_photo = None
    if isinstance(image, dict):
        profile_photo = image.get("contentUrl") or image.get("url")
    elif isinstance(image, str):
        profile_photo = image

    return {
        "name": person.get("name"),
        "first_name": person.get("givenName"),
        "last_name": person.get("familyName"),
        "position": person.get("headline"),
        "company": company,
        "location": location,
        "profile_photo": profile_photo,
    }


def _extract_person_from_any_script(html: str) -> Optional[Dict]:
    """Busca en el HTML scripts JSON-LD con datos de Person."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        if script.string:
            try:
                row = _parse_person_from_json_ld(json.loads(script.string))
                if row and (row.get("name") or row.get("position") or row.get("company")):
                    return row
            except (json.JSONDecodeError, TypeError):
                pass
    for match in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        content = match.group(1).strip()
        if "Person" not in content and "headline" not in content and "givenName" not in content:
            continue
        try:
            parsed = json.loads(content)
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                    if isinstance(item, dict):
                        row = _parse_person_from_json_ld(item)
                        if row and (row.get("name") or row.get("position") or row.get("company")):
                            return row
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _extract_person_from_dom(driver) -> Optional[Dict]:
    """Extrae nombre, headline, ubicación y empresa desde el DOM de LinkedIn."""
    try:
        out = {
            "name": None, "first_name": None, "last_name": None,
            "position": None, "company": None, "location": None,
        }
        name_el = driver.find_elements(By.CSS_SELECTOR, "h1.text-heading-xlarge, h1.inline.t-24")
        if name_el:
            out["name"] = name_el[0].text.strip() or None
        headline_el = driver.find_elements(By.CSS_SELECTOR, "div.text-body-medium.break-words, div.inline.t-14")
        if headline_el:
            out["position"] = headline_el[0].text.strip() or None
        for sel in (
            "span.text-body-small.inline.t-black--light",
            "div.text-body-small.inline.t-black--light",
            "span.inline.t-black--light.break-words",
        ):
            loc_el = driver.find_elements(By.CSS_SELECTOR, sel)
            if loc_el and loc_el[0].text.strip():
                txt = loc_el[0].text.strip()
                if len(txt) < 200 and not txt.startswith("http"):
                    out["location"] = txt
                    break
        if out["position"] and " at " in out["position"]:
            out["company"] = out["position"].split(" at ")[-1].strip()
        if out.get("name") or out.get("position") or out.get("location") or out.get("company"):
            return out
    except Exception:
        pass
    return None


def _is_valid_phone(text: str) -> bool:
    """
    Verifica si un texto es un número de teléfono válido.
    - Sin letras ni acentos (excluye "Móvil", "Trabajo", etc.)
    - Sin puntos (los separan de versiones como 1.13.42781)
    - Al menos 7 dígitos
    - Solo contiene dígitos, espacios, guiones, paréntesis y el prefijo +
    """
    if not text or len(text) > 25:
        return False
    if re.search(r'[a-zA-ZÀ-ÿ]', text):
        return False
    if "." in text:  # versiones (1.13.42781) u otros formatos no telefónicos
        return False
    digits = re.sub(r'\D', '', text)
    return len(digits) >= 7 and bool(re.match(r'^[\+\d][\d\s\-\(\)]+$', text))


def _extract_contact_info_from_overlay(driver, slug: str) -> Dict:
    """
    Navega al overlay de información de contacto del perfil y extrae email y teléfono.

    Estructura real del overlay de LinkedIn:
      <h3 class="pv-contact-info__header …">\\n  Teléfono\\n</h3>
      <ul class="list-style-none">
        <li>
          <span class="t-14 t-black t-normal">653329820</span>
          <span class="t-14 t-black--light t-normal">(Trabajo)</span>
        </li>
      </ul>

    Para el email: enlaces <a href="mailto:…"> (fiable).
    Para el teléfono: XPath al <h3> que contenga "Teléfono"/"Phone" → <ul> siguiente
    → <span class="t-14 t-black t-normal"> (el que NO tiene t-black--light).
    """
    result: Dict = {"emails": None, "phones": None}
    try:
        overlay_url = f"https://www.linkedin.com/in/{slug}/overlay/contact-info/"
        driver.get(overlay_url)
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "div.pv-contact-info, h3.pv-contact-info__header, a[href^='mailto:']")
                )
            )
        except Exception:
            time.sleep(3)

        # ── Emails: enlaces mailto: ───────────────────────────────────────────────
        emails = []
        for a in driver.find_elements(By.CSS_SELECTOR, "a[href^='mailto:']"):
            href = a.get_attribute("href") or ""
            addr = href.replace("mailto:", "").strip()
            if addr and "@" in addr and "linkedin.com" not in addr and addr not in emails:
                emails.append(addr)

        # ── Teléfonos: XPath al h3 con texto "Teléfono"/"Phone" ──────────────────
        # La estructura del overlay tiene el h3 y la ul como HERMANOS dentro del mismo
        # contenedor. El h3 tiene espacios/saltos alrededor del texto, por eso se usa
        # normalize-space() en lugar de text()=.
        phones = []
        phone_xpath = (
            "//h3[contains(normalize-space(.), 'Teléfono') "
            "or contains(normalize-space(.), 'Phone') "
            "or contains(normalize-space(.), 'Tel')]"
        )
        for h3 in driver.find_elements(By.XPATH, phone_xpath):
            try:
                # El <ul> con los números está como hermano siguiente del <h3>
                ul = h3.find_element(By.XPATH, "following-sibling::ul[1]")
                # Span con la clase t-black t-normal = el número (no la etiqueta "Móvil")
                for span in ul.find_elements(
                    By.CSS_SELECTOR,
                    "span.t-14.t-black.t-normal, span.t-black.t-normal"
                ):
                    text = span.text.strip()
                    if _is_valid_phone(text) and text not in phones:
                        phones.append(text)
            except Exception:
                pass

        # Fallback: si XPath no encontró nada, buscar en BeautifulSoup con regex sin anclas
        if not phones:
            soup = BeautifulSoup(driver.page_source, "html.parser")
            for header in soup.find_all(
                string=re.compile(r"Tel[eé]fono|Phone|Tel\b", re.IGNORECASE)
            ):
                # Navegar hasta el contenedor padre que tenga hermanos con el número
                node = header.parent
                for _ in range(5):
                    if node is None:
                        break
                    sibling = node.find_next_sibling("ul")
                    if sibling:
                        for span in sibling.find_all("span"):
                            text = span.get_text(strip=True)
                            if _is_valid_phone(text) and text not in phones:
                                phones.append(text)
                        break
                    node = node.parent

        if emails:
            result["emails"] = "; ".join(emails)
        if phones:
            result["phones"] = "; ".join(phones[:3])

    except Exception as e:
        _log.debug("_extract_contact_info_from_overlay (%s) falló: %s", slug, e)

    return result


def _extract_extra_from_dom(driver) -> Dict:
    """
    Extrae del DOM del perfil los campos que no están en JSON-LD:
    profile_photo, followers, connections, premium, creator, open_to_work.
    """
    result: Dict = {
        "profile_photo": None,
        "followers": None,
        "connections": None,
        "premium": None,
        "creator": None,
        "open_to_work": None,
    }
    try:
        # ── Foto de perfil ────────────────────────────────────────────────────
        for sel in [
            "img.pv-top-card-profile-picture__image-v2",
            "img.profile-photo-edit__preview",
            "img[class*='profile-picture']",
            "button.pv-top-card-profile-picture__edit-overlay img",
        ]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                src = els[0].get_attribute("src") or ""
                if src and "ghost" not in src and "static" not in src:
                    result["profile_photo"] = src
                    break

        # ── Conexiones y seguidores ───────────────────────────────────────────
        # LinkedIn muestra "X seguidores" y "X contactos" en el perfil
        page_text = driver.find_element(By.TAG_NAME, "body").text
        for pattern, key in [
            (r'([\d,\.]+\s*(?:K|M)?)\s*(?:followers|seguidores)', "followers"),
            (r'([\d,\.]+\+?)\s*(?:connections?|contactos)', "connections"),
        ]:
            m = re.search(pattern, page_text, re.IGNORECASE)
            if m:
                result[key] = m.group(1).strip()

        # ── Premium ───────────────────────────────────────────────────────────
        premium_els = driver.find_elements(
            By.CSS_SELECTOR,
            "li-icon[type*='premium'], .premium-icon, [aria-label*='Premium'], [class*='premium-badge']"
        )
        result["premium"] = len(premium_els) > 0 or None

        # ── Creator ───────────────────────────────────────────────────────────
        creator_els = driver.find_elements(
            By.CSS_SELECTOR, "[class*='creator-badge'], [aria-label*='Creator']"
        )
        if not creator_els:
            creator_els = [el for el in driver.find_elements(By.CSS_SELECTOR, "span.t-14")
                           if "creator" in (el.text or "").lower()]
        result["creator"] = len(creator_els) > 0 or None

        # ── Open to work ──────────────────────────────────────────────────────
        otw_els = driver.find_elements(
            By.CSS_SELECTOR,
            "#open-to-work-overlay-text, [class*='open-to-work'], [aria-label*='Open to work']"
        )
        if not otw_els:
            otw_els = [el for el in driver.find_elements(By.CSS_SELECTOR, "span.t-14, div.t-14")
                       if "open to work" in (el.text or "").lower()
                       or "abierto a trabajar" in (el.text or "").lower()]
        result["open_to_work"] = len(otw_els) > 0 or None

    except Exception as e:
        _log.debug("_extract_extra_from_dom falló: %s", e)

    return result


def _extract_internal_id_from_html(html: str, public_id: Optional[str] = None) -> Optional[str]:
    """Extrae el id interno (ACoA…) del perfil desde el HTML renderizado."""
    if not html:
        return None
    acoa_pat = re.compile(r'(ACoA[A-Za-z0-9_-]{22,})')
    if public_id:
        public_escaped = re.escape(public_id)
        for m in re.finditer(rf'publicIdentifier["\']?\s*:\s*["\']?{public_escaped}', html):
            start = max(0, m.start() - 500)
            end = min(len(html), m.end() + 3000)
            chunk = html[start:end]
            aco = acoa_pat.search(chunk)
            if aco:
                return aco.group(1)
        for m in re.finditer(public_escaped, html):
            start = m.start()
            end = min(len(html), m.end() + 2500)
            chunk = html[start:end]
            aco = acoa_pat.search(chunk)
            if aco:
                return aco.group(1)
        return None
    patterns = [
        r'urn:li:fsd_profile:(ACoA[A-Za-z0-9_-]{20,})',
        r'"profileId"\s*:\s*"(ACoA[A-Za-z0-9_-]{20,})"',
        r'entityUrn["\']?\s*:\s*["\']?urn:li:fsd_profile:(ACoA[A-Za-z0-9_-]+)',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


# ── Driver con cookies ─────────────────────────────────────────────────────────

def _create_driver_with_cookies(
    session: LinkedInSession,
    headless: Optional[bool] = None,
    proxy: Optional[str] = None,
):
    """
    Crea un WebDriver con las cookies de la sesión ya inyectadas.
    Si headless=None usa la variable de entorno HEADLESS (por defecto True).
    proxy: 'host:port' o 'user:pass@host:port' para enrutar el tráfico por un proxy.
    Aplica el script stealth antes de la primera navegación.
    Devuelve el driver listo para navegar, o None si no se puede crear.
    Usa 'eager' page load strategy y timeout de 45s para servidores con poca RAM.
    """
    use_headless = HEADLESS if headless is None else headless
    try:
        opts = _make_chrome_options(headless=use_headless, proxy=proxy)
        opts.page_load_strategy = "eager"
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(30)
    except Exception as e:
        _log.error("No se pudo crear el WebDriver: %s", e)
        return None
    try:
        _apply_stealth(driver)
        # Cargamos solo la página base de LinkedIn para poder inyectar cookies
        # (las cookies solo se pueden inyectar si el dominio ya está cargado).
        # NO cargamos /feed/ aquí — eso lo haría cada visita de perfil.
        # La sesión ya fue validada en init_client, no hace falta revalidar.
        driver.get("https://www.linkedin.com")
        time.sleep(random.uniform(0.8, 1.2))
        _inject_cookies(driver, session.cookies)
        return driver
    except Exception as e:
        _log.error("Error inyectando cookies en el driver: %s", e)
        try:
            driver.quit()
        except Exception:
            pass
        return None


# ── Scraping de perfil ─────────────────────────────────────────────────────────

def _scrape_profile_via_browser(
    session: LinkedInSession, url: str, public_id: str, driver=None
) -> Tuple[Optional[Dict], list]:
    """
    Carga el perfil con Selenium y extrae datos desde JSON-LD y/o el DOM.
    Si se proporciona `driver`, lo usa sin cerrarlo al acabar.
    Si no, crea uno propio y lo cierra al finalizar.
    Devuelve (dict_perfil_normalizado, []).
    """
    owned = driver is None
    if owned:
        driver = _create_driver_with_cookies(session)
    if not driver:
        return None, []
    try:
        driver.get(url)
        try:
            WebDriverWait(driver, BROWSER_PROFILE_WAIT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception:
            pass
        # Segunda carga para que la SPA inyecte el JSON-LD
        driver.refresh()
        time.sleep(3)
        try:
            WebDriverWait(driver, BROWSER_PROFILE_WAIT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception:
            pass
        html = driver.page_source
        for _ in range(BROWSER_PROFILE_WAIT):
            if "application/ld+json" in html or ('"givenName"' in html and '"headline"' in html):
                break
            time.sleep(1)
            html = driver.page_source

        row = _extract_person_from_any_script(html)
        if not row:
            row = _extract_person_from_dom(driver)

        if row and (row.get("name") or row.get("position") or row.get("company")):
            return {
                "profile_id": public_id,
                "name": row.get("name"),
                "first_name": row.get("first_name"),
                "last_name": row.get("last_name"),
                "position": row.get("position"),
                "company": row.get("company"),
                "location": row.get("location"),
                "emails": None,
                "phones": None,
                "is_connection": None,
                "followers": None,
                "connections": None,
                "profile_link": url,
                "profile_photo": row.get("profile_photo"),
                "premium": None,
                "creator": None,
                "open_to_work": None,
            }, []
        return None, []
    finally:
        if owned:
            try:
                driver.quit()
            except Exception:
                pass


# ── Scraping de conexiones ─────────────────────────────────────────────────────

def _build_connection_dict(slug: str, name: Optional[str], position: Optional[str]) -> Dict:
    """Construye el dict normalizado de una conexión."""
    return {
        "profile_id": slug,
        "name": name,
        "first_name": None,
        "last_name": None,
        "position": position,
        "company": None,
        "location": None,
        "emails": None,
        "phones": None,
        "is_connection": True,
        "followers": None,
        "connections": None,
        "profile_link": f"https://www.linkedin.com/in/{slug}/",
        "profile_photo": None,
        "premium": None,
        "creator": None,
        "open_to_work": None,
    }


def _extract_connection_cards_from_driver(driver) -> list:
    """
    Extrae las tarjetas de conexión visibles en el DOM.
    Prueba en orden:
    1. Selectores específicos de /mynetwork/ (li.mn-connection-card)
    2. Selectores de /search/results/people/
    3. Fallback genérico: todos los a[href*="/in/"]
    """
    results = []
    seen_slugs: set = set()

    # 1) Página de conexiones (/mynetwork/)
    cards = driver.find_elements(By.CSS_SELECTOR, "li.mn-connection-card")
    if not cards:
        cards = driver.find_elements(By.CSS_SELECTOR, "li[class*='connection-card']")

    if cards:
        for card in cards:
            try:
                link_els = card.find_elements(
                    By.CSS_SELECTOR, "a.mn-connection-card__link, a[href*='/in/']"
                )
                if not link_els:
                    continue
                href = link_els[0].get_attribute("href") or ""
                m = re.search(r"linkedin\.com/in/([^/?#]+)", href)
                if not m:
                    continue
                slug = m.group(1).rstrip("/").lower()
                if not slug or len(slug) < 2 or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)

                name = None
                for sel in ["span.mn-connection-card__name", "span[class*='name']", ".actor-name"]:
                    els = card.find_elements(By.CSS_SELECTOR, sel)
                    if els and els[0].text.strip():
                        name = els[0].text.strip()
                        break

                position = None
                for sel in [
                    "span.mn-connection-card__occupation",
                    "p.mn-connection-card__occupation",
                    "p[class*='occupation']",
                    "span[class*='occupation']",
                ]:
                    els = card.find_elements(By.CSS_SELECTOR, sel)
                    if els and els[0].text.strip():
                        position = els[0].text.strip()
                        break

                results.append(_build_connection_dict(slug, name, position))
            except Exception:
                continue
        return results

    # 2) Página de búsqueda (/search/results/people/)
    search_cards = driver.find_elements(
        By.CSS_SELECTOR,
        "li.reusable-search__result-container, li[class*='result-container']",
    )
    if search_cards:
        for card in search_cards:
            try:
                link_els = card.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")
                if not link_els:
                    continue
                href = link_els[0].get_attribute("href") or ""
                m = re.search(r"linkedin\.com/in/([^/?#]+)", href)
                if not m:
                    continue
                slug = m.group(1).rstrip("/").lower()
                if not slug or len(slug) < 2 or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)

                name = None
                for sel in ["span[class*='actor-name']", "span.t-16", "span[aria-hidden='true']"]:
                    els = card.find_elements(By.CSS_SELECTOR, sel)
                    if els and els[0].text.strip():
                        txt = els[0].text.strip()
                        if len(txt) < 80 and "\n" not in txt:
                            name = txt
                            break

                position = None
                for sel in [
                    "div.entity-result__primary-subtitle",
                    "div[class*='primary-subtitle']",
                    "div[class*='subtitle']",
                ]:
                    els = card.find_elements(By.CSS_SELECTOR, sel)
                    if els and els[0].text.strip():
                        position = els[0].text.strip()
                        break

                results.append(_build_connection_dict(slug, name, position))
            except Exception:
                continue
        return results

    # 3) Fallback genérico
    links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")
    for a in links:
        try:
            href = a.get_attribute("href") or ""
            m = re.search(r"linkedin\.com/in/([^/?#]+)", href)
            if not m:
                continue
            slug = m.group(1).rstrip("/").lower()
            if not slug or len(slug) < 2 or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            name = a.text.strip() or None
            if name and len(name) > 120:
                name = None
            results.append(_build_connection_dict(slug, name, None))
        except Exception:
            continue
    return results


def _collect_connection_slugs(driver, max_contacts: int) -> List[str]:
    """
    Navega por la página de conexiones y la página de búsqueda de primer grado
    para recopilar slugs únicos hasta alcanzar max_contacts.
    No extrae datos de cada perfil aquí — eso lo hace _enrich_connection_from_profile.
    """
    seen: set = set()
    slugs: List[str] = []

    def _extract_slugs_from_page() -> int:
        """Extrae slugs de los enlaces /in/ visibles en la página actual. Devuelve cuántos nuevos."""
        new = 0
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")
        for a in links:
            if len(slugs) >= max_contacts:
                break
            try:
                href = a.get_attribute("href") or ""
                m = re.search(r"linkedin\.com/in/([^/?#]+)", href)
                if not m:
                    continue
                slug = m.group(1).rstrip("/").lower()
                # Excluir slugs que sean del propio usuario, menús u otras secciones
                if not slug or len(slug) < 2 or slug in seen:
                    continue
                seen.add(slug)
                slugs.append(slug)
                new += 1
            except Exception:
                continue
        return new

    # Intentar primero la página de conexiones (/mynetwork/catch-up/connections/)
    _log.info("Slug collection: cargando %s", _CONNECTIONS_URL)
    driver.get(_CONNECTIONS_URL)
    time.sleep(random.uniform(3.0, 5.0))

    no_progress = 0
    for scroll_i in range(max(20, max_contacts // 5 + 10)):
        if len(slugs) >= max_contacts:
            break
        prev = len(slugs)
        _extract_slugs_from_page()
        if len(slugs) == prev:
            no_progress += 1
            if no_progress >= 4:
                break
            time.sleep(random.uniform(1.5, 3.0))
        else:
            no_progress = 0

        steps = random.randint(2, 4)
        for _ in range(steps):
            driver.execute_script(f"window.scrollBy(0, {random.randint(300, 600)});")
            time.sleep(random.uniform(0.2, 0.5))
        time.sleep(random.uniform(1.0, 2.0))

    if len(slugs) < max_contacts:
        # Fallback: búsqueda de conexiones de primer grado
        _log.info("Slug collection: fallback a búsqueda (%d/%d hasta ahora)", len(slugs), max_contacts)
        driver.get(_CONNECTIONS_SEARCH_URL)
        time.sleep(random.uniform(3.5, 5.5))
        for _ in range(max(8, max_contacts // 8 + 5)):
            if len(slugs) >= max_contacts:
                break
            _extract_slugs_from_page()
            driver.execute_script("window.scrollBy(0, 700);")
            time.sleep(random.uniform(1.5, 3.0))

    _log.info("Slugs recopilados: %d", len(slugs))
    return slugs[:max_contacts]


def _enrich_connection_from_profile(driver, slug: str) -> Dict:
    """
    Visita el perfil de una conexión (y su overlay de contacto) usando el driver activo
    para extraer todos los campos del CSV: nombre, posición, empresa, ubicación,
    email, teléfono, foto, seguidores, conexiones, premium, creator, open_to_work.
    """
    url = f"https://www.linkedin.com/in/{slug}/"
    try:
        # ── 1. Cargar página de perfil ─────────────────────────────────────────
        driver.get(url)
        try:
            WebDriverWait(driver, BROWSER_PROFILE_WAIT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception:
            pass
        # Esperar a que la SPA inyecte JSON-LD estructurado
        for _ in range(10):
            html = driver.page_source
            if "application/ld+json" in html or '"givenName"' in html:
                break
            time.sleep(1)

        html = driver.page_source

        # ── 2. Extraer datos estructurados (JSON-LD) ───────────────────────────
        row = _extract_person_from_any_script(html)
        if not row:
            row = _extract_person_from_dom(driver)

        # ── 3. Extraer campos extra del DOM (foto, seguidores, premium, etc.) ──
        extra = _extract_extra_from_dom(driver)

        # ── 4. Visitar overlay de información de contacto ──────────────────────
        time.sleep(random.uniform(1.5, 3.0))
        contact = _extract_contact_info_from_overlay(driver, slug)

        # ── 5. Construir resultado completo ────────────────────────────────────
        result = _build_connection_dict(
            slug,
            row.get("name") if row else None,
            row.get("position") if row else None,
        )
        if row:
            result["first_name"] = row.get("first_name")
            result["last_name"] = row.get("last_name")
            result["company"] = row.get("company")
            result["location"] = row.get("location")
            # profile_photo puede venir del JSON-LD o del DOM
            result["profile_photo"] = row.get("profile_photo") or extra.get("profile_photo")
        else:
            result["profile_photo"] = extra.get("profile_photo")

        result["emails"] = contact.get("emails")
        result["phones"] = contact.get("phones")
        result["followers"] = extra.get("followers")
        result["connections"] = extra.get("connections")
        result["premium"] = extra.get("premium")
        result["creator"] = extra.get("creator")
        result["open_to_work"] = extra.get("open_to_work")
        result["is_connection"] = True
        result["profile_link"] = url

        _log.debug(
            "Enriquecido %s: name=%s pos=%s company=%s email=%s phone=%s",
            slug, result.get("name"), result.get("position"),
            result.get("company"), result.get("emails"), result.get("phones"),
        )
        return result

    except Exception as e:
        _log.warning("Error enriqueciendo %s: %s", slug, e)
        return _build_connection_dict(slug, None, None)


def scrape_connections_selenium(
    session: LinkedInSession, max_contacts: int, driver=None
) -> pd.DataFrame:
    """
    Scrapea las conexiones de LinkedIn usando Selenium con las cookies de sesión.

    Si se proporciona `driver`, lo usa sin cerrarlo al acabar (driver compartido).
    Si no, crea uno propio y lo cierra al finalizar.

    Estrategia en dos fases:
    1. Recopilación de slugs: navega por /mynetwork/catch-up/connections/ con scroll
       para obtener todos los slugs de conexiones. Si no consigue suficientes, usa
       la búsqueda de primer grado como fallback.
    2. Enriquecimiento: visita el perfil de cada conexión para extraer nombre,
       posición, empresa, ubicación, etc. (datos que la lista no muestra).

    Si detecta authwall, login o soft-block, marca session.on_block y devuelve vacío.
    """
    _log.info("Iniciando scraping de conexiones con Selenium (máx. %d)...", max_contacts)
    owned = driver is None
    if owned:
        driver = _create_driver_with_cookies(session)
    if not driver:
        _log.error("Selenium: no se pudo crear el WebDriver")
        return pd.DataFrame()

    try:
        # ── Comprobación inicial de sesión ─────────────────────────────────────
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(random.uniform(2.0, 3.5))
        current_url = driver.current_url
        if any(kw in current_url for kw in ("authwall", "/login", "checkpoint", "uas/login")):
            _log.warning("Selenium: redirigido a '%s', sesión no válida", current_url)
            session.on_block = True
            return pd.DataFrame()
        if _is_soft_blocked(driver):
            _log.warning("Selenium: soft-block detectado en el feed")
            print("⚠️  LinkedIn muestra captcha/verificación. Espera unos minutos.")
            session.on_block = True
            return pd.DataFrame()

        # ── FASE 1: recopilar slugs ─────────────────────────────────────────────
        print(f"   Fase 1/2: recopilando slugs de {max_contacts} conexiones...")
        slugs = _collect_connection_slugs(driver, max_contacts)
        print(f"   Fase 1/2: {len(slugs)} slugs obtenidos.")

        if not slugs:
            _log.warning("Selenium: 0 slugs encontrados")
            return pd.DataFrame()

        # ── FASE 2: enriquecer cada perfil ──────────────────────────────────────
        print(f"   Fase 2/2: visitando perfiles para extraer datos completos...")
        enriched: List[Dict] = []
        for i, slug in enumerate(slugs):
            # Comprobar soft-block periódicamente
            if _is_soft_blocked(driver):
                _log.warning("Selenium: soft-block durante enriquecimiento en perfil %d/%d", i + 1, len(slugs))
                print(f"\n⚠️  LinkedIn mostró verificación/captcha en perfil {i+1}. Se detiene el scraping.")
                session.on_block = True
                break

            print(f"   Perfil {i + 1}/{len(slugs)}: {slug}", end="\r", flush=True)
            conn = _enrich_connection_from_profile(driver, slug)
            enriched.append(conn)

            # Pausa anti-detección entre visitas a perfiles
            if i < len(slugs) - 1:
                pause = random.uniform(4.0, 9.0)
                time.sleep(pause)

        print()  # nueva línea tras el \r de progreso
        _log.info("Selenium: %d conexiones enriquecidas", len(enriched))

        if not enriched:
            return pd.DataFrame()
        return pd.DataFrame(enriched)

    except Exception as e:
        _log.error("Error en scrape_connections_selenium: %s", e)
        return pd.DataFrame()
    finally:
        if owned:
            try:
                driver.quit()
            except Exception:
                pass


# ── API pública del módulo ─────────────────────────────────────────────────────

def scrape_connections(
    session: LinkedInSession, max_contacts: int, driver=None
) -> pd.DataFrame:
    """Scrapea las conexiones de tu cuenta usando Selenium."""
    print(f"\n👥 Scrapeando conexiones (máx. {max_contacts})...")
    return scrape_connections_selenium(session, max_contacts, driver=driver)


def scrape_profile_and_connections(
    session: LinkedInSession, username: str, max_contacts: int
) -> Tuple[Dict, pd.DataFrame]:
    """
    Orquestador: scrapea el perfil propio + conexiones con un único driver Chrome.
    Abre el navegador una sola vez, lo reutiliza en todo el proceso y lo cierra al final.
    Devuelve (dict_perfil, DataFrame_conexiones).
    """
    profile_url = f"https://www.linkedin.com/in/{username}/"
    perfil: Optional[Dict] = None

    # Abrir un único driver para todo el proceso (perfil + conexiones)
    driver = _create_driver_with_cookies(session)
    if not driver:
        _log.error("No se pudo crear el WebDriver para scrape_profile_and_connections")
        return {
            "profile_id": username, "name": None, "first_name": None,
            "last_name": None, "position": None, "company": None,
            "location": None, "emails": None, "phones": None,
            "is_connection": None, "followers": None, "connections": None,
            "profile_link": profile_url, "profile_photo": None,
            "premium": None, "creator": None, "open_to_work": None,
            "scrape_error": "No se pudo crear el WebDriver",
        }, pd.DataFrame()

    try:
        # ── Perfil ────────────────────────────────────────────────────────────
        print(f"\n📋 Scrapeando perfil: {username}")
        try:
            result = _scrape_profile_via_browser(session, profile_url, username, driver=driver)
            if result and result[0]:
                perfil = result[0]
        except Exception as exc:
            _log.warning("Error scrapeando perfil '%s': %s", username, exc)

        if perfil is None:
            _log.warning("No se pudo obtener el perfil '%s'. Continuando con las conexiones.", username)
            print(f"⚠️  No se pudo obtener el perfil '{username}'. Continuando con las conexiones...")
        perfil = {
            "profile_id": username,
                "name": None, "first_name": None, "last_name": None,
                "position": None, "company": None, "location": None,
                "emails": None, "phones": None, "is_connection": None,
                "followers": None, "connections": None,
                "profile_link": profile_url, "profile_photo": None,
                "premium": None, "creator": None, "open_to_work": None,
                "scrape_error": "No se pudo obtener el perfil",
            }

        # ── Pausa entre perfil y conexiones ───────────────────────────────────
        pause = random.uniform(4.0, 8.0)
        _log.debug("Pausa de %.1fs entre perfil y conexiones (anti-detección)", pause)
        time.sleep(pause)

        # ── Conexiones (mismo driver, sin cerrar y reabrir Chrome) ────────────
        conexiones = scrape_connections(session, max_contacts, driver=driver)

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return perfil, conexiones


# ── Fase A: recopilar índice de slugs ──────────────────────────────────────────

def collect_all_slugs(session: LinkedInSession, proxy: Optional[str] = None) -> List[str]:
    """
    Fase A del scraping en producción: recorre la página de conexiones y la
    búsqueda de primer grado para recopilar TODOS los slugs disponibles
    sin visitar ningún perfil individual (rápido, sin enriquecimiento).

    proxy: proxy a usar para esta sesión ('host:port' o 'user:pass@host:port').
    Devuelve la lista de slugs únicos encontrados.
    """
    _log.info("collect_all_slugs: iniciando recopilación del índice de conexiones")
    driver = _create_driver_with_cookies(session, proxy=proxy)
    if not driver:
        _log.error("collect_all_slugs: no se pudo crear el WebDriver")
        return []

    # Slugs propios a excluir (el perfil del usuario logueado y strings no-slug)
    OWN_SLUG = (session.username or "").lower()
    EXCLUDED = {"me", "login", "feed", "jobs", "messaging", "notifications",
                "search", "mynetwork", "in", "company", "school", ""}

    seen: set = set()
    slugs: List[str] = []

    def _harvest_page() -> int:
        """Extrae slugs de todos los enlaces /in/ visibles en la página actual."""
        new = 0
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/in/']")
        for a in links:
            try:
                href = a.get_attribute("href") or ""
                m = re.search(r"linkedin\.com/in/([^/?#]+)", href)
                if not m:
                    continue
                slug = m.group(1).rstrip("/").lower()
                if not slug or len(slug) < 2 or slug in seen:
                    continue
                if slug in EXCLUDED or slug == OWN_SLUG:
                    continue
                seen.add(slug)
                slugs.append(slug)
                new += 1
            except Exception:
                continue
        return new

    try:
        # ── 1. Página de conexiones ────────────────────────────────────────────
        _log.info("collect_all_slugs: cargando %s", _CONNECTIONS_URL)
        driver.get(_CONNECTIONS_URL)
        time.sleep(random.uniform(3.0, 5.0))

        if any(kw in driver.current_url for kw in ("authwall", "/login", "checkpoint")):
            _log.warning("collect_all_slugs: sesión no válida, redirigido a login")
            session.on_block = True
            return []

        no_progress = 0
        for _ in range(60):  # máx. 60 scrolls (~600 conexiones aprox.)
            prev = len(slugs)
            _harvest_page()
            if len(slugs) == prev:
                no_progress += 1
                if no_progress >= 5:
                    _log.info("collect_all_slugs: sin nuevos slugs tras 5 rondas en /mynetwork/")
                    break
                time.sleep(random.uniform(1.5, 3.0))
            else:
                no_progress = 0

            steps = random.randint(3, 5)
            for _ in range(steps):
                driver.execute_script(f"window.scrollBy(0, {random.randint(300, 600)});")
                time.sleep(random.uniform(0.15, 0.4))
            time.sleep(random.uniform(0.8, 1.8))

        _log.info("collect_all_slugs: %d slugs tras /mynetwork/", len(slugs))

        # ── 2. Búsqueda de primer grado (complementa /mynetwork/) ────────────
        _log.info("collect_all_slugs: cargando búsqueda de primer grado")
        driver.get(_CONNECTIONS_SEARCH_URL)
        time.sleep(random.uniform(3.5, 5.5))

        no_progress = 0
        for _ in range(40):
            prev = len(slugs)
            _harvest_page()
            if len(slugs) == prev:
                no_progress += 1
                if no_progress >= 5:
                    break
                time.sleep(random.uniform(1.5, 2.5))
            else:
                no_progress = 0
            driver.execute_script(f"window.scrollBy(0, {random.randint(400, 700)});")
            time.sleep(random.uniform(1.0, 2.0))

        _log.info("collect_all_slugs: %d slugs totales recopilados", len(slugs))

    except Exception as e:
        _log.error("collect_all_slugs: error inesperado: %s", e)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return slugs
