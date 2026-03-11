# scraper.py
import os
import re
import json
import time
from typing import Dict, Optional, Tuple

import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from staffspy import LinkedInAccount
from staffspy.linkedin.certifications import CertificationFetcher
from staffspy.linkedin.contact_info import ContactInfoFetcher
from staffspy.linkedin.linkedin import LinkedInScraper
from staffspy.utils.models import Staff

load_dotenv()

# Tiempo de espera al cargar el perfil en el navegador (segundos)
BROWSER_PROFILE_WAIT = int(os.getenv("BROWSER_PROFILE_WAIT", "10"))


def _disable_staffspy_certifications() -> None:
    """Desactiva el scraping de certificaciones para evitar errores internos.

    StaffSpy está rompiendo actualmente al intentar parsear certificaciones
    (cambios en LinkedIn). Como a ti ahora mismo no te interesan las
    certificaciones, las anulamos para poder mantener extra_profile_data=True
    y seguir obteniendo, cuando exista, info rica de perfil/contacto.
    """

    def _noop_fetch_certifications(self, staff):
        # No hacemos nada, pero devolvemos True para que el flujo continúe
        staff.certifications = []
        return True

    CertificationFetcher.fetch_certifications = _noop_fetch_certifications


_disable_staffspy_certifications()


def _get_env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


LOG_LEVEL = _get_env_int("LOG_LEVEL", 1)
SLEEP_TIME = _get_env_int("SLEEP_TIME", 4)


def init_client() -> LinkedInAccount:
    """Inicializa el cliente de LinkedIn usando StaffSpy."""
    print("🔐 Conectando a LinkedIn...")
    account = LinkedInAccount(
        session_file="session.pkl",  # guarda login, solo pide credenciales una vez
        log_level=LOG_LEVEL,
    )
    return account


def _normalize_emails(data: Dict) -> str | None:
    """Unifica emails reales + potenciales en un solo campo de texto."""
    emails: list[str] = []

    # Email directo de la conexión (cuando lo tiene público)
    email_direct = data.get("connection_email") or data.get("email_address")
    if email_direct:
        emails.append(str(email_direct).strip())

    # Emails potenciales generados por StaffSpy
    for key in ("potential_email", "potential_emails"):
        raw = data.get(key)
        if not raw:
            continue
        parts = [p.strip() for p in str(raw).split(",") if p.strip()]
        emails.extend(parts)

    emails = sorted(set(emails))
    return ", ".join(emails) if emails else None


def _normalize_phones(data: Dict) -> str | None:
    """Convierte la info de teléfonos a un string legible."""
    phones = data.get("connection_phone_numbers") or data.get("phone_numbers")
    if not phones:
        return None
    return str(phones)


def _normalize_company(data: Dict) -> str | None:
    """Intenta obtener la empresa actual."""
    return data.get("company") or data.get("current_company")


def _normalize_location(data: Dict) -> str | None:
    """Devuelve la localización tal cual la da StaffSpy."""
    return data.get("location")


def normalize_profile_row(row: pd.Series) -> Dict:
    """Normaliza una fila de StaffSpy a un diccionario sencillo."""
    data = row.to_dict()
    normalized = {
        "profile_id": data.get("profile_id") or data.get("id"),
        "name": data.get("name"),
        "first_name": data.get("first_name"),
        "last_name": data.get("last_name"),
        "position": data.get("position"),
        "company": _normalize_company(data),
        "location": _normalize_location(data),
        "emails": _normalize_emails(data),
        "phones": _normalize_phones(data),
        "is_connection": data.get("is_connection"),
        "followers": data.get("followers"),
        "connections": data.get("connections"),
        "profile_link": data.get("profile_link"),
        "profile_photo": data.get("profile_photo"),
        "premium": data.get("premium"),
        "creator": data.get("creator"),
        "open_to_work": data.get("open_to_work"),
    }
    return normalized


PROFILE_VIEW_EP = "https://www.linkedin.com/voyager/api/identity/profiles/{user_id}/profileView"


def _fetch_and_merge_contact_info(
    account: LinkedInAccount,
    internal_id: str,
    urn: str,
    public_id: str,
    url: str,
    result: Dict,
) -> None:
    """Pide email/teléfono al endpoint de contact info y los escribe en result (solo si es tu conexión)."""
    contact_ep = "https://www.linkedin.com/voyager/api/graphql?queryId=voyagerIdentityDashProfiles.13618f886ce95bf503079f49245fbd6f&queryName=ProfilesByMemberIdentity&variables=(memberIdentity:{employee_id},count:1)"
    ep = contact_ep.format(employee_id=internal_id)
    try:
        res = account.session.get(ep)
        print(f"   [LOG] Contact info API: status={res.status_code} internal_id={internal_id[:20]}...")
        if res.status_code != 200:
            print(f"   [LOG] Contact info response (primeros 300 chars): {res.text[:300]}")
        else:
            try:
                data = res.json()
                elements = (data.get("data") or {}).get("identityDashProfilesByMemberIdentity", {}).get("elements") or []
                print(f"   [LOG] Contact info elements count: {len(elements)}")
                if elements:
                    first = elements[0]
                    print(f"   [LOG] Keys en element: {list(first.keys())[:15]}")
                    if first.get("emailAddress"):
                        result["emails"] = first.get("emailAddress", {}).get("emailAddress")
                        print(f"   [LOG] Email extraído: {result['emails']}")
                    ph = first.get("phoneNumbers") or []
                    if ph:
                        result["phones"] = ", ".join(p.get("phoneNumber", {}).get("number", "") for p in ph if isinstance(p, dict))
                        print(f"   [LOG] Teléfonos extraídos: {result['phones']}")
                    # Ubicación (prioritaria) y nombre/apellido desde el mismo endpoint
                    if first.get("address") and not result.get("location"):
                        addr = first["address"]
                        if isinstance(addr, str):
                            result["location"] = addr.strip() or None
                        elif isinstance(addr, dict):
                            parts = [
                                addr.get("city") or addr.get("cityName"),
                                addr.get("region"),
                                addr.get("country") or addr.get("countryName"),
                                addr.get("line1"),
                                addr.get("formatted") or addr.get("formattedAddress"),
                            ]
                            result["location"] = ", ".join(filter(None, (str(p).strip() for p in parts))) or None
                        if result.get("location"):
                            print(f"   [LOG] Ubicación extraída: {result['location']}")
                    if first.get("firstName") and not result.get("first_name"):
                        result["first_name"] = first.get("firstName", "").strip() or None
                    if first.get("lastName") and not result.get("last_name"):
                        result["last_name"] = first.get("lastName", "").strip() or None
                    if (result.get("first_name") or result.get("last_name")) and not result.get("name"):
                        result["name"] = " ".join(filter(None, [result.get("first_name"), result.get("last_name")]))
                else:
                    print(f"   [LOG] Response data (recorte): {str(data)[:400]}")
            except Exception as e:
                print(f"   [LOG] Error parseando contact info JSON: {e}")
        # Intentar también con StaffSpy por si el formato cambió
        staff = Staff(
            id=internal_id,
            urn=urn,
            search_term="url",
            profile_id=public_id,
            profile_link=url,
        )
        ContactInfoFetcher(account.session).fetch_contact_info(staff)
        if staff.contact_info:
            if getattr(staff.contact_info, "email_address", None):
                result["emails"] = staff.contact_info.email_address
            if getattr(staff.contact_info, "phone_numbers", None) and staff.contact_info.phone_numbers:
                result["phones"] = ", ".join(staff.contact_info.phone_numbers)
            if not result.get("location") and getattr(staff.contact_info, "address", None):
                addr = staff.contact_info.address
                result["location"] = addr if isinstance(addr, str) else (addr.get("formatted") or addr.get("line1") or str(addr))
        if result.get("emails") or result.get("phones"):
            result["is_connection"] = True
    except Exception as e:
        print(f"   [LOG] Excepción en _fetch_and_merge_contact_info: {e}")


def _extract_internal_id_from_html(html: str, public_id: Optional[str] = None) -> Optional[str]:
    """Extrae el id interno del perfil visitado (ACoA...), no el del usuario logueado.

    Si se pasa public_id (ej. kirian-pla-bonete-9a75031b1), se busca el ACoA solo en el
    contexto donde aparece ese perfil (p. ej. junto a publicIdentifier), para no devolver
    el ID del viewer.
    """
    if not html:
        return None
    acoa_pat = re.compile(r'(ACoA[A-Za-z0-9_-]{22,})')

    if public_id:
        # Buscar bloques que contengan el public_id del perfil visitado y extraer ACoA ahí
        # Así evitamos el ACoA del usuario logueado (suele ser el primero en la página)
        public_escaped = re.escape(public_id)
        # "publicIdentifier":"kirian-pla-bonete-9a75031b1" o similar
        for m in re.finditer(rf'publicIdentifier["\']?\s*:\s*["\']?{public_escaped}', html):
            start = max(0, m.start() - 500)
            end = min(len(html), m.end() + 3000)
            chunk = html[start:end]
            aco = acoa_pat.search(chunk)
            if aco:
                return aco.group(1)
        # Por si el objeto tiene primero profileId y luego publicIdentifier: buscar ventana tras el slug
        for m in re.finditer(public_escaped, html):
            start = m.start()
            end = min(len(html), m.end() + 2500)
            chunk = html[start:end]
            aco = acoa_pat.search(chunk)
            if aco:
                return aco.group(1)
        return None

    # Sin public_id: comportamiento legacy (puede devolver el ID del viewer)
    patterns = [
        r'urn:li:fsd_profile:(ACoA[A-Za-z0-9_-]{20,})',
        r'"profileId"\s*:\s*"(ACoA[A-Za-z0-9_-]{20,})"',
        r'entityUrn["\']?\s*:\s*["\']?urn:li:fsd_profile:(ACoA[A-Za-z0-9_-]+)',
        r'memberIdentity["\']?\s*:\s*["\']?(ACoA[A-Za-z0-9_-]{20,})',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def _extract_internal_id_from_profile_html(session, profile_url: str, public_id: Optional[str] = None) -> Optional[str]:
    """Extrae el id interno del perfil visitado (ACoA...) desde el HTML de la página de LinkedIn."""
    try:
        resp = session.get(profile_url)
        if not resp.ok:
            return None
        html = resp.text
    except Exception:
        return None
    if not public_id:
        m = re.search(r"linkedin\.com/in/([^/?]+)", profile_url)
        public_id = m.group(1).rstrip("/") if m else None
    return _extract_internal_id_from_html(html, public_id)


def _find_in_dict(obj, *keys: str) -> Optional[str]:
    """Busca la primera clave que exista en un dict anidado (una sola profundidad por clave)."""
    if not isinstance(obj, dict):
        return None
    for key in keys:
        if key in obj:
            val = obj[key]
            if isinstance(val, str) and val:
                return val
    return None


def _deep_find_value(obj, target_key: str) -> Optional[str]:
    """Recorre recursivamente dict/list y devuelve el primer valor para target_key."""
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


def _resolve_public_id_to_internal(
    session, public_id: str, profile_view_data: Optional[dict] = None
) -> Optional[Tuple[str, str]]:
    """Devuelve (id_interno, urn) desde el JSON de profileView, o None si no hay."""
    if profile_view_data is None:
        try:
            resp = session.get(PROFILE_VIEW_EP.format(user_id=public_id))
            resp.raise_for_status()
            profile_view_data = resp.json()
        except Exception:
            return None
    data = profile_view_data

    profile_id = None
    urn = None

    # Rutas fijas (StaffSpy)
    profile_id = (data.get("positionView") or {}).get("profileId")
    mini = (data.get("profile") or {}).get("miniProfile") or {}
    raw_urn = mini.get("objectUrn") or mini.get("entityUrn")
    if isinstance(raw_urn, str) and raw_urn:
        parts = raw_urn.split(":")
        urn = parts[-1] if parts else None
        if not profile_id and ("fsd_profile" in raw_urn or "member" in raw_urn):
            profile_id = urn

    # Si LinkedIn cambió la estructura, buscar en todo el JSON
    if not profile_id:
        profile_id = _deep_find_value(data, "profileId") or _deep_find_value(data, "entityUrn")
        if profile_id and ":" in profile_id:
            profile_id = profile_id.split(":")[-1]
    if not urn and profile_id:
        urn = profile_id
    if not urn:
        urn = _deep_find_value(data, "objectUrn")
        if urn and ":" in urn:
            urn = urn.split(":")[-1]

    # Respuesta en formato "included" (ej. GraphQL/JSON:API)
    if not profile_id and isinstance(data.get("included"), list):
        for item in data["included"]:
            if not isinstance(item, dict):
                continue
            pid = item.get("profileId") or item.get("entityUrn") or item.get("id")
            if pid and isinstance(pid, str):
                if ":" in pid:
                    pid = pid.split(":")[-1]
                if len(pid) >= 15:
                    profile_id = pid
                    if not urn:
                        urn = pid
                    break

    if profile_id and urn:
        return (str(profile_id), str(urn))
    return None


def _extract_internal_id_from_profile_view(data: dict) -> Optional[str]:
    """Extrae el id interno (ACoA...) del JSON de profileView si la resolución estándar falló."""
    if not data:
        return None
    # Buscar en todo el JSON un valor que parezca ID interno de miembro
    found = _deep_find_value(data, "profileId") or _deep_find_value(data, "entityUrn")
    if found and isinstance(found, str):
        if ":" in found:
            found = found.split(":")[-1]
        if found.startswith("ACoA") and len(found) >= 20:
            return found
    # Búsqueda por patrón en el JSON serializado (último recurso)
    try:
        raw = json.dumps(data)
        match = re.search(r'"ACoA[A-Za-z0-9_-]{20,}"', raw)
        if match:
            return match.group(0).strip('"')
    except Exception:
        pass
    return None


def _extract_from_profile_view_json(data: dict) -> Dict:
    """Extrae nombre, headline, etc. del JSON de profileView aunque no tengamos id/urn."""
    out = {
        "name": None,
        "first_name": None,
        "last_name": None,
        "position": None,
        "company": None,
        "location": None,
    }
    profile = data.get("profile") or {}
    if isinstance(profile, dict):
        out["position"] = _find_in_dict(profile, "headline")
        if not out["position"] and isinstance(profile.get("headlineV2"), dict):
            t = (profile["headlineV2"].get("text") or {})
            if isinstance(t, dict):
                out["position"] = t.get("text")
        out["first_name"] = _find_in_dict(profile, "firstName", "firstId")
        out["last_name"] = _find_in_dict(profile, "lastName", "lastId")
        if out["first_name"] or out["last_name"]:
            out["name"] = " ".join(filter(None, [out["first_name"], out["last_name"]]))
    mini = profile.get("miniProfile") or {}
    if isinstance(mini, dict) and not out["name"]:
        out["name"] = _find_in_dict(mini, "firstName", "lastName")
    loc = _deep_find_value(data, "geoLocationName") or _deep_find_value(data, "locationName")
    if loc:
        out["location"] = loc
    return out


def _parse_person_from_json_ld(parsed: dict) -> Optional[Dict]:
    """Extrae datos de Person desde JSON-LD (formato ScrapFly / LinkedIn público).
    https://scrapfly.io/blog/posts/how-to-scrape-linkedin
    """
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
    return {
        "name": person.get("name"),
        "first_name": person.get("givenName"),
        "last_name": person.get("familyName"),
        "position": person.get("headline"),
        "company": company,
        "location": location,
    }


def _extract_person_from_any_script(html: str) -> Optional[Dict]:
    """Busca en el HTML cualquier script que contenga JSON con datos de Person (JSON-LD u otro)."""
    # 1) Scripts con type="application/ld+json"
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        if script.string:
            try:
                row = _parse_person_from_json_ld(json.loads(script.string))
                if row and (row.get("name") or row.get("position") or row.get("company")):
                    return row
            except (json.JSONDecodeError, TypeError):
                pass
    # 2) Buscar en todo el HTML bloques que parezcan JSON con Person/headline/givenName
    for match in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        content = match.group(1).strip()
        if "Person" not in content and "headline" not in content and "givenName" not in content:
            continue
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        row = _parse_person_from_json_ld(item)
                        if row and (row.get("name") or row.get("position") or row.get("company")):
                            return row
            else:
                row = _parse_person_from_json_ld(parsed)
                if row and (row.get("name") or row.get("position") or row.get("company")):
                    return row
        except (json.JSONDecodeError, TypeError, NameError):
            pass
    return None


def _extract_person_from_dom(driver) -> Optional[Dict]:
    """Extrae nombre, headline, ubicación y empresa desde el DOM (selectores típicos de LinkedIn)."""
    try:
        from selenium.webdriver.common.by import By
        out = {"name": None, "first_name": None, "last_name": None, "position": None, "company": None, "location": None}
        name_el = driver.find_elements(By.CSS_SELECTOR, "h1.text-heading-xlarge, h1.inline.t-24")
        if name_el:
            out["name"] = name_el[0].text.strip() or None
        headline_el = driver.find_elements(By.CSS_SELECTOR, "div.text-body-medium.break-words, div.inline.t-14")
        if headline_el:
            out["position"] = headline_el[0].text.strip() or None
        # Ubicación: span bajo el headline (LinkedIn suele usar text-body-small o similar)
        for sel in (
            "span.text-body-small.inline.t-black--light",
            "div.text-body-small.inline.t-black--light",
            "[data-test-id='profile-contact-info-location']",
            "span.inline.t-black--light.break-words",
        ):
            loc_el = driver.find_elements(By.CSS_SELECTOR, sel)
            if loc_el and loc_el[0].text.strip():
                txt = loc_el[0].text.strip()
                if len(txt) < 200 and not txt.startswith("http"):  # evita ruido
                    out["location"] = txt
                    break
        # Empresa: a veces en el headline ("Role at Company") o en la primera experiencia
        if out["position"] and " at " in out["position"]:
            out["company"] = out["position"].split(" at ")[-1].strip()
        for sel in (
            "section[data-section='experience'] div.display-flex span[aria-hidden='true']",
            "div#experience-section ~ div a[href*='/company/']",
            "a[data-field='experience_company_logo']",
        ):
            try:
                company_el = driver.find_elements(By.CSS_SELECTOR, sel)
                if company_el and company_el[0].text.strip() and not out.get("company"):
                    out["company"] = company_el[0].text.strip()[:200]
                    break
            except Exception:
                pass
        if out.get("name") or out.get("position") or out.get("location") or out.get("company"):
            return out
    except Exception:
        pass
    return None


def _extract_suggested_connections_from_page(driver, current_public_id: str) -> list:
    """Extrae de la página del perfil los enlaces a 'conexiones en común' y 'personas que quizá conozcas'.

    LinkedIn muestra en el lateral (o en la página) un bloque con conexiones en común y a veces
    sugerencias (PYMK). Buscamos todos los enlaces a perfiles /in/SLUG que no sean el perfil
    actual y los devolvemos con source='mutual' o 'pymk' si logramos identificar el bloque.
    """
    from selenium.webdriver.common.by import By

    result = []
    seen = set()
    current_public_id = (current_public_id or "").strip().lower()

    try:
        # Pequeño scroll para que el sidebar cargue (contenido lazy)
        driver.execute_script("window.scrollTo(0, 400);")
        time.sleep(1)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)

        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='linkedin.com/in/']")
        for a in links:
            try:
                href = a.get_attribute("href") or ""
                m = re.search(r"linkedin\.com/in/([^/?]+)", href)
                if not m:
                    continue
                slug = m.group(1).rstrip("/").lower()
                if slug == current_public_id or len(slug) < 3:
                    continue
                if slug in seen:
                    continue
                seen.add(slug)
                name = (a.text or "").strip()
                if len(name) > 120:
                    name = name[:120]
                # Intentar etiquetar: si el enlace está en un contenedor con "mutual" o "conexiones en común" -> mutual
                source = "suggested"
                try:
                    parent = a.find_element(By.XPATH, "./ancestor::*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'mutual') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'conexiones en común') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'conexión en común')][1]")
                    if parent:
                        source = "mutual"
                except Exception:
                    try:
                        parent = a.find_element(By.XPATH, "./ancestor::*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'personas que quizá') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'people you may know')][1]")
                        if parent:
                            source = "pymk"
                    except Exception:
                        pass
                result.append({
                    "profile_id": slug,
                    "name": name or None,
                    "profile_link": f"https://www.linkedin.com/in/{slug}/",
                    "source": source,
                })
            except Exception:
                continue
    except Exception:
        pass
    return result


def _scrape_profile_via_browser(
    account: LinkedInAccount, url: str, public_id: str
) -> Tuple[Optional[Dict], list]:
    """Carga el perfil en un navegador real con las cookies de la sesión y extrae JSON-LD.

    LinkedIn sirve el JSON-LD cuando la página se renderiza.
    También extrae la lista de conexiones en común / personas que quizá conozcas si aparecen.
    Devuelve (dict_perfil, lista_sugeridos).
    """
    driver = None
    try:
        from staffspy.utils.utils import get_webdriver
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By
    except ImportError:
        return None, []
    try:
        driver = get_webdriver(None)
    except Exception:
        driver = None
    if not driver:
        try:
            opts = ChromeOptions()
            opts.add_argument("--headless=new")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            driver = webdriver.Chrome(options=opts)
        except Exception as e:
            print(f"   (Navegador no disponible para fallback: {e})")
            return None, []
    try:
        driver.get("https://www.linkedin.com")
        time.sleep(2)
        for c in account.session.cookies:
            domain = c.domain if c.domain else ".linkedin.com"
            if not domain.startswith(".") and "linkedin" in domain:
                domain = "." + domain
            try:
                driver.add_cookie({"name": c.name, "value": c.value, "domain": domain})
            except Exception:
                pass
        # Recargar la home para que las cookies se apliquen y no salga "error al cargar"
        driver.refresh()
        time.sleep(2)
        driver.get(url)
        try:
            WebDriverWait(driver, BROWSER_PROFILE_WAIT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception:
            pass
        # Recarga automática del perfil: la primera carga a veces muestra "error al cargar";
        # la segunda ya va con sesión y muestra el perfil (evita tener que pulsar recargar a mano)
        driver.refresh()
        time.sleep(3)
        try:
            WebDriverWait(driver, BROWSER_PROFILE_WAIT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception:
            pass
        # Esperar a que la SPA inyecte JSON-LD (LinkedIn puede cargarlo tras unos segundos)
        html = driver.page_source
        for _ in range(BROWSER_PROFILE_WAIT):
            if "application/ld+json" in html or ('"givenName"' in html and '"headline"' in html):
                break
            time.sleep(1)
            html = driver.page_source
        if "authwall" in html.lower() or "login" in html.lower() and "linkedin.com/in" in url:
            pass
        row = _extract_person_from_any_script(html)
        if not row:
            row = _extract_person_from_dom(driver)
        # ID interno del perfil visitado (no del viewer), desde el HTML renderizado
        internal_id = _extract_internal_id_from_html(html, public_id)
        if internal_id:
            print(f"   [LOG] ID interno desde HTML del navegador: {internal_id[:25]}...")
        if row and (row.get("name") or row.get("position") or row.get("company")):
            out = {
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
                "profile_photo": None,
                "premium": None,
                "creator": None,
                "open_to_work": None,
            }
            if internal_id:
                out["_internal_id"] = internal_id  # clave interna, no se guarda en CSV
            # Extraer conexiones en común / personas que quizá conozcas de la misma página
            suggested = _extract_suggested_connections_from_page(driver, public_id)
            if suggested:
                print(f"   [LOG] Encontradas {len(suggested)} sugerencias (conexiones en común / PYMK)")
            return out, suggested
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
    return None, []


def _scrape_profile_by_url_fallback(
    account: LinkedInAccount, url: str, public_id: str, profile_view_json: Optional[dict] = None
) -> Dict:
    """Fallback: mismos campos que conexiones; datos desde profileView JSON y/o HTML JSON-LD."""
    # Misma estructura que normalize_profile_row para que el CSV tenga las mismas columnas
    data: Dict = {
        "profile_id": public_id,
        "name": None,
        "first_name": None,
        "last_name": None,
        "position": None,
        "company": None,
        "location": None,
        "emails": None,
        "phones": None,
        "is_connection": None,
        "followers": None,
        "connections": None,
        "profile_link": url,
        "profile_photo": None,
        "premium": None,
        "creator": None,
        "open_to_work": None,
    }
    if profile_view_json:
        extracted = _extract_from_profile_view_json(profile_view_json)
        data.update({k: v for k, v in extracted.items() if v is not None})
    # Complementar con JSON-LD del HTML si hay
    try:
        resp = account.session.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        script_tag = soup.find("script", type="application/ld+json")
        if script_tag and script_tag.string:
            parsed = json.loads(script_tag.string)
            person = None
            if isinstance(parsed, dict) and "@graph" in parsed:
                person = next((x for x in parsed["@graph"] if x.get("@type") == "Person"), None)
            elif isinstance(parsed, dict):
                person = parsed
            if person:
                if not data.get("name"):
                    data["name"] = person.get("name")
                if not data.get("first_name"):
                    data["first_name"] = person.get("givenName")
                if not data.get("last_name"):
                    data["last_name"] = person.get("familyName")
                if not data.get("position"):
                    data["position"] = person.get("headline")
                if not data.get("company"):
                    w = person.get("worksFor")
                    if isinstance(w, list) and w:
                        data["company"] = w[0].get("name")
                    elif isinstance(w, dict):
                        data["company"] = w.get("name")
                if not data.get("location"):
                    addr = person.get("address") or {}
                    if isinstance(addr, dict):
                        data["location"] = ", ".join(
                            filter(None, [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]))
                    if not data.get("location"):
                        data["location"] = None
    except Exception:
        pass
    return data


def scrape_profile_by_url(account: LinkedInAccount, url: str) -> Tuple[Dict, list]:
    """Scrapea un perfil por URL con los mismos campos que las conexiones.

    Primero intenta el API interno (profileView + fetch_all_info_for_employee)
    para obtener nombre, empresa, ubicación, email/teléfono si está visible.
    Si no se puede resolver id/urn, extrae lo posible del JSON de profileView y del HTML.
    Cuando se usa el navegador, además extrae conexiones en común / personas que quizá conozcas.
    Devuelve (dict_perfil, lista_sugeridos).
    """
    print(f"\n📄 Scrapeando perfil por URL:\n   {url}")
    m = re.search(r"linkedin\.com/in/([^/?]+)", url)
    public_id = m.group(1).rstrip("/") if m else ""

    # Obtener profileView una vez para usarlo en resolución y en fallback
    profile_view_json = None
    try:
        r = account.session.get(PROFILE_VIEW_EP.format(user_id=public_id))
        if r.ok:
            profile_view_json = r.json()
        print(f"   [LOG] profileView API: status={r.status_code}, json={'ok' if profile_view_json else 'no'}")
    except Exception as e:
        print(f"   [LOG] profileView API error: {e}")

    resolved = _resolve_public_id_to_internal(account.session, public_id, profile_view_json) if profile_view_json else None
    if resolved:
        print(f"   [LOG] ID interno resuelto por API: {resolved[0][:25]}...")
    if not resolved:
        # Intentar extraer id interno desde el HTML de la página del perfil
        internal_id = _extract_internal_id_from_profile_html(account.session, url, public_id)
        if internal_id:
            resolved = (internal_id, internal_id)
            print(f"   [LOG] ID interno resuelto por HTML: {internal_id[:25]}...")
    if not resolved and profile_view_json:
        internal_id = _extract_internal_id_from_profile_view(profile_view_json)
        if internal_id:
            resolved = (internal_id, internal_id)
            print(f"   [LOG] ID interno extraído de profileView JSON: {internal_id[:25]}...")
    if not resolved:
        print(f"   [LOG] No se pudo obtener ID interno (ni API ni HTML ni profileView)")
    if resolved:
        internal_id, urn = resolved
        staff = Staff(
            id=internal_id,
            urn=urn,
            search_term="url",
            profile_id=public_id,
            profile_link=url,
        )
        li_scraper = LinkedInScraper(account.session)
        li_scraper.num_staff = 1
        try:
            li_scraper.fetch_all_info_for_employee(staff, 1)
            # StaffSpy solo pide email/teléfono cuando is_connection=True (lista de conexiones).
            # Para perfil por URL, pedimos contact info a mano; LinkedIn solo lo devuelve si es tu conexión.
            try:
                ContactInfoFetcher(account.session).fetch_contact_info(staff)
            except Exception:
                pass
            return normalize_profile_row(pd.Series(staff.to_dict())), []
        except Exception as e:
            print(f"   [LOG] fetch_all_info_for_employee falló: {e}")
            pass

    # Obtener ID interno para contact info (usar en fallback y en resultado navegador)
    cid, curn = None, None
    if resolved:
        cid, curn = internal_id, urn
    elif profile_view_json:
        cid = _extract_internal_id_from_profile_view(profile_view_json)
        curn = cid
    if not cid:
        cid = _extract_internal_id_from_profile_html(account.session, url, public_id)
        curn = cid

    # Fallback 1: extraer del JSON de profileView y/o HTML con requests
    result = _scrape_profile_by_url_fallback(account, url, public_id, profile_view_json)
    has_data = result.get("name") or result.get("position") or result.get("company")
    # Pedir contact info si tenemos ID interno
    if cid and curn:
        print(f"   [LOG] Pidiendo contact info para internal_id={cid[:25]}...")
        _fetch_and_merge_contact_info(account, cid, curn, public_id, url, result)
    else:
        print(f"   [LOG] No hay ID interno para contact info (resolved={bool(resolved)}, profile_view_json={bool(profile_view_json)})")
    if has_data:
        return result, []

    # Fallback 2: cargar perfil en navegador real (como ScrapFly / linkedin-mcp-server)
    # LinkedIn suele incluir JSON-LD solo cuando la página se renderiza; además extraemos sugeridos
    print("   ⚠️  Probando con navegador (Chrome headless) para extraer JSON-LD del perfil…")
    browser_result, suggested = _scrape_profile_via_browser(account, url, public_id)
    if browser_result and (browser_result.get("name") or browser_result.get("position") or browser_result.get("company")):
        # Contact info: usar ID del navegador si no teníamos uno (profileView 410)
        bid = browser_result.pop("_internal_id", None)
        if bid:
            cid, curn = bid, bid
        if cid and curn:
            print(f"   [LOG] Fusionando contact info en resultado del navegador (internal_id={cid[:25]}...)")
            _fetch_and_merge_contact_info(account, cid, curn, public_id, url, browser_result)
        return browser_result, suggested

    if not has_data:
        print("   ⚠️  No se pudo obtener datos del perfil (API, HTML ni navegador).")
    return result, []


def scrape_single_profile(account: LinkedInAccount, username: str) -> Dict:
    """Scrapea y normaliza un único perfil por username de LinkedIn."""
    print(f"\n📋 Scrapeando perfil: {username}")
    try:
        users = account.scrape_users(user_ids=[username])
    except Exception as exc:  # StaffSpy puede romperse si LinkedIn cambia
        raise RuntimeError(
            f"No se pudo scrapear el usuario '{username}' con StaffSpy "
            f"(scrape_users falló): {exc}"
        ) from exc

    if users.empty:
        raise ValueError(f"No se pudo obtener el perfil de {username}")

    row = users.iloc[0]
    return normalize_profile_row(row)


def scrape_connections(
    account: LinkedInAccount, max_contacts: int
) -> pd.DataFrame:
    """Scrapea y normaliza las conexiones de tu cuenta."""
    print(f"\n👥 Scrapeando conexiones (máx. {max_contacts})...")
    df = account.scrape_connections(
        extra_profile_data=True,
        max_results=max_contacts,
    )
    if df.empty:
        return df

    normalized_rows = [normalize_profile_row(row) for _, row in df.iterrows()]
    return pd.DataFrame(normalized_rows)


def scrape_profile_and_connections(
    account: LinkedInAccount, username: str, max_contacts: int
) -> Tuple[Dict, pd.DataFrame]:
    """Orquestador: devuelve perfil principal (si se puede) + conexiones.

    Si StaffSpy falla al scrapear el perfil (scrape_users roto por cambios
    en LinkedIn), seguimos igualmente con las conexiones y devolvemos
    un perfil mínimo con el error adjunto.
    """
    try:
        perfil = scrape_single_profile(account, username)
    except Exception as exc:
        print(
            f"⚠️  No se pudo scrapear el perfil '{username}' con StaffSpy. "
            "Probablemente LinkedIn haya cambiado su estructura.\n"
            "   Seguimos igualmente con las conexiones de TU cuenta.\n"
            f"   Detalle técnico: {exc}"
        )
        perfil = {
            "profile_id": username,
            "name": None,
            "company": None,
            "location": None,
            "emails": None,
            "phones": None,
            "scrape_error": str(exc),
        }

    conexiones = scrape_connections(account, max_contacts)
    return perfil, conexiones
