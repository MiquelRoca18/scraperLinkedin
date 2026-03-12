"""
Tests del módulo scraper: misma lógica que cuando se ejecuta contra LinkedIn.
Sin llamadas a la red; se simulan respuestas de StaffSpy.
"""
import pandas as pd
import pytest
import requests

from scraper import (
    _find_public_identifier,
    normalize_profile_row,
    scrape_single_profile,
    scrape_connections,
    scrape_profile_and_connections,
)
from tests.conftest import COLUMNAS_CSV_CONEXIONES


# ----- _find_public_identifier -----


def test_find_public_identifier_directo():
    assert _find_public_identifier({"publicIdentifier": "juan-perez"}) == "juan-perez"


def test_find_public_identifier_con_espacios():
    assert _find_public_identifier({"publicIdentifier": "  juan-perez  "}) == "juan-perez"


def test_find_public_identifier_anidado():
    assert _find_public_identifier({"data": {"profile": {"publicIdentifier": "maria-lopez"}}}) == "maria-lopez"


def test_find_public_identifier_en_lista():
    assert _find_public_identifier([{"x": 1}, {"publicIdentifier": "ana-garcia"}]) == "ana-garcia"


def test_find_public_identifier_no_encontrado():
    assert _find_public_identifier({"name": "Juan"}) is None
    assert _find_public_identifier({}) is None
    assert _find_public_identifier([]) is None


def test_find_public_identifier_vacio_o_invalido():
    assert _find_public_identifier({"publicIdentifier": ""}) is None
    assert _find_public_identifier({"publicIdentifier": None}) is None


# ----- normalize_profile_row -----


def test_normalize_profile_row_completo(staff_row_completo):
    out = normalize_profile_row(staff_row_completo)
    assert out["profile_id"] == "juan-garcia-123"
    assert out["name"] == "Juan García"
    assert out["company"] == "Tech SA"
    assert out["location"] == "Barcelona, Spain"
    assert out["position"] == "Software Engineer"
    assert out["is_connection"] == "yes"
    assert out["followers"] == 500
    assert out["connections"] == 500
    assert out["profile_link"] == "https://www.linkedin.com/in/juan-garcia-123"
    assert out["premium"] is False
    assert out["open_to_work"] is True
    # Emails: connection_email + potential_email unificados
    assert "juan@empresa.com" in (out["emails"] or "")
    assert "juan.garcia@tech.com" in (out["emails"] or "")
    # Phones
    assert out["phones"] == "['+34600123456']"


def test_normalize_profile_row_minimo(staff_row_minimo):
    out = normalize_profile_row(staff_row_minimo)
    assert out["profile_id"] == "maria-lopez"
    assert out["name"] == "María López"
    assert out["profile_link"] == "https://www.linkedin.com/in/maria-lopez"
    assert out["company"] is None
    assert out["emails"] is None
    assert out["phones"] is None


def test_normalize_profile_row_usa_id_si_no_profile_id():
    row = pd.Series({"id": "ACoAAA123", "name": "X", "profile_link": "https://linkedin.com/in/x"})
    out = normalize_profile_row(row)
    assert out["profile_id"] == "ACoAAA123"


def test_normalize_profile_row_emails_alternativos(staff_row_solo_emails_alternativos):
    out = normalize_profile_row(staff_row_solo_emails_alternativos)
    assert out["emails"] is not None
    assert "ana@mail.com" in out["emails"]
    assert "a.martin@empresa.com" in out["emails"]
    assert "ana.martin@empresa.com" in out["emails"]


def test_normalize_profile_row_company_current_company():
    row = pd.Series({"profile_id": "x", "current_company": "Fallback SA", "profile_link": "https://x"})
    out = normalize_profile_row(row)
    assert out["company"] == "Fallback SA"


def test_normalize_profile_row_phones_connection_phone_numbers():
    row = pd.Series({
        "profile_id": "x", "name": "X",
        "connection_phone_numbers": "+34 600 111 222",
        "profile_link": "https://x",
    })
    out = normalize_profile_row(row)
    assert out["phones"] == "+34 600 111 222"


def test_normalize_profile_row_tiene_exactamente_columnas_del_csv_conexiones():
    """El dict normalizado debe tener exactamente las mismas columnas que output/conexiones_*_*.csv."""
    row = pd.Series({
        "profile_id": "test", "name": "Test", "profile_link": "https://linkedin.com/in/test",
    })
    out = normalize_profile_row(row)
    assert list(out.keys()) == COLUMNAS_CSV_CONEXIONES
    assert out["profile_id"] == "test"
    assert out["name"] == "Test"


def test_normalize_profile_row_tiene_columnas_esperadas_para_csv():
    """El dict normalizado debe tener las columnas que main escribe al CSV."""
    row = pd.Series({
        "profile_id": "test", "name": "Test", "profile_link": "https://linkedin.com/in/test",
    })
    out = normalize_profile_row(row)
    for c in COLUMNAS_CSV_CONEXIONES:
        assert c in out
    assert out["profile_id"] == "test"
    assert out["name"] == "Test"


def test_normalize_profile_row_datos_como_csv_real_email_nombre_telefono(staff_row_como_csv_real):
    """
    Con una fila con la forma del CSV real (connection_email, phone_numbers, name, etc.),
    el scraper debe sacar emails, nombre y teléfono en las columnas correctas.
    """
    out = normalize_profile_row(staff_row_como_csv_real)
    assert out["profile_id"] == "arturo-garcía-serna-ruiz"
    assert out["name"] == "Arturo García-Serna Ruiz"
    assert out["first_name"] == "Arturo"
    assert out["last_name"] == "García-Serna Ruiz"
    assert out["location"] == "Dublin, County Dublin, Ireland"
    assert out["emails"] == "agarciasernaruiz@linkedin.com"
    assert out["phones"] == "['+353(86)2148158']"
    assert out["is_connection"] == "yes"
    assert out["followers"] == 16210
    assert out["connections"] == 15269
    assert out["premium"] is True
    assert out["creator"] is True
    assert out["open_to_work"] is False
    # Todas las columnas del CSV deben existir
    assert list(out.keys()) == COLUMNAS_CSV_CONEXIONES


def test_scrape_connections_dataframe_tiene_columnas_del_csv():
    """El DataFrame devuelto por scrape_connections debe tener exactamente las columnas del CSV de conexiones."""
    class FakeAccount:
        on_block = False
        def scrape_connections(self, extra_profile_data, max_results):
            return pd.DataFrame([{
                "profile_id": "c1",
                "name": "Contacto Uno",
                "first_name": "Contacto",
                "last_name": "Uno",
                "position": "Dev",
                "company": "Acme",
                "location": "Madrid",
                "connection_email": "c1@acme.com",
                "phone_numbers": "['612000000']",
                "is_connection": "yes",
                "followers": 100,
                "connections": 200,
                "profile_link": "https://linkedin.com/in/c1",
                "profile_photo": None,
                "premium": False,
                "creator": False,
                "open_to_work": False,
            }])

    df = scrape_connections(FakeAccount(), max_contacts=10)
    assert len(df) == 1
    assert list(df.columns) == COLUMNAS_CSV_CONEXIONES
    assert df.iloc[0]["name"] == "Contacto Uno"
    assert df.iloc[0]["emails"] == "c1@acme.com"
    assert "612000000" in str(df.iloc[0]["phones"])


def test_normalize_profile_row_acepta_nan_o_valores_faltantes():
    """Filas con NaN o keys faltantes no deben romper."""
    row = pd.Series({
        "profile_id": "x",
        "name": "X",
        "company": float("nan"),
        "profile_link": "https://x",
    })
    out = normalize_profile_row(row)
    assert out["profile_id"] == "x"
    assert out["company"] is None or (isinstance(out["company"], float) and out["company"] != out["company"])


# ----- scrape_single_profile (con cuenta fake) -----


def test_scrape_single_profile_ok():
    class FakeAccount:
        def scrape_users(self, user_ids):
            return pd.DataFrame([{
                "profile_id": "test-user",
                "name": "Test User",
                "company": "Test Co",
                "profile_link": "https://www.linkedin.com/in/test-user",
            }])

    perfil = scrape_single_profile(FakeAccount(), "test-user")
    assert perfil["profile_id"] == "test-user"
    assert perfil["name"] == "Test User"
    assert perfil["company"] == "Test Co"


def test_scrape_single_profile_scrape_users_lanza():
    class FakeAccount:
        def scrape_users(self, user_ids):
            raise ValueError("Failed to find user_id")

    with pytest.raises(RuntimeError) as exc_info:
        scrape_single_profile(FakeAccount(), "bad-user")
    assert "bad-user" in str(exc_info.value)
    assert "scrape_users falló" in str(exc_info.value)


def test_scrape_single_profile_scrape_users_devuelve_vacio():
    class FakeAccount:
        def scrape_users(self, user_ids):
            return pd.DataFrame()

    with pytest.raises(ValueError) as exc_info:
        scrape_single_profile(FakeAccount(), "empty-user")
    assert "No se pudo obtener el perfil" in str(exc_info.value)


def test_scrape_single_profile_scrape_users_toomanyredirects():
    class FakeAccount:
        def scrape_users(self, user_ids):
            raise requests.exceptions.TooManyRedirects("Exceeded 30 redirects")

    with pytest.raises(RuntimeError):
        scrape_single_profile(FakeAccount(), "redirect-user")


# ----- scrape_connections (con cuenta fake) -----


def test_scrape_connections_ok():
    class FakeAccount:
        on_block = False
        def scrape_connections(self, extra_profile_data, max_results):
            return pd.DataFrame([
                {"profile_id": "c1", "name": "Conexión 1", "company": "C1", "profile_link": "https://linkedin.com/in/c1"},
                {"profile_id": "c2", "name": "Conexión 2", "company": "C2", "profile_link": "https://linkedin.com/in/c2"},
            ])

    df = scrape_connections(FakeAccount(), max_contacts=10)
    assert len(df) == 2
    assert list(df["profile_id"]) == ["c1", "c2"]
    assert list(df["name"]) == ["Conexión 1", "Conexión 2"]


def test_scrape_connections_vacio():
    class FakeAccount:
        on_block = False
        def scrape_connections(self, extra_profile_data, max_results):
            return pd.DataFrame()

    df = scrape_connections(FakeAccount(), max_contacts=10)
    assert df.empty


def test_scrape_connections_toomanyredirects_activa_on_block():
    class FakeAccount:
        on_block = False
        def scrape_connections(self, extra_profile_data, max_results):
            raise requests.exceptions.TooManyRedirects("Exceeded 30 redirects")

    acc = FakeAccount()
    df = scrape_connections(acc, max_contacts=10)
    assert df.empty
    assert acc.on_block is True


def test_scrape_connections_otra_excepcion_propaga():
    class FakeAccount:
        on_block = False
        def scrape_connections(self, extra_profile_data, max_results):
            raise RuntimeError("Otro error")

    with pytest.raises(RuntimeError):
        scrape_connections(FakeAccount(), max_contacts=10)


# ----- scrape_profile_and_connections (orquestador) -----


def test_scrape_profile_and_connections_todo_ok():
    class FakeAccount:
        on_block = False
        def scrape_users(self, user_ids):
            return pd.DataFrame([{"profile_id": "me", "name": "Yo", "company": "Mi Co", "profile_link": "https://linkedin.com/in/me"}])
        def scrape_connections(self, extra_profile_data, max_results):
            return pd.DataFrame([{"profile_id": "c1", "name": "Contacto", "profile_link": "https://linkedin.com/in/c1"}])

    perfil, conexiones = scrape_profile_and_connections(FakeAccount(), "me", max_contacts=5)
    assert perfil["profile_id"] == "me"
    assert perfil["name"] == "Yo"
    assert "scrape_error" not in perfil
    assert len(conexiones) == 1
    assert conexiones.iloc[0]["name"] == "Contacto"


def test_scrape_profile_and_connections_perfil_falla_conexiones_ok():
    class FakeAccount:
        on_block = False
        def scrape_users(self, user_ids):
            raise ValueError("Failed to find user_id")
        def scrape_connections(self, extra_profile_data, max_results):
            return pd.DataFrame([{"profile_id": "c1", "name": "Contacto", "profile_link": "https://linkedin.com/in/c1"}])

    perfil, conexiones = scrape_profile_and_connections(FakeAccount(), "bad-user", max_contacts=5)
    assert perfil["profile_id"] == "bad-user"
    assert perfil.get("name") is None
    assert "scrape_error" in perfil
    assert len(conexiones) == 1


def test_scrape_profile_and_connections_perfil_ok_conexiones_vacio():
    class FakeAccount:
        on_block = False
        def scrape_users(self, user_ids):
            return pd.DataFrame([{"profile_id": "me", "name": "Yo", "profile_link": "https://linkedin.com/in/me"}])
        def scrape_connections(self, extra_profile_data, max_results):
            return pd.DataFrame()

    perfil, conexiones = scrape_profile_and_connections(FakeAccount(), "me", max_contacts=5)
    assert perfil["profile_id"] == "me"
    assert conexiones.empty


def test_scrape_profile_and_connections_perfil_falla_conexiones_vacio():
    class FakeAccount:
        on_block = False
        def scrape_users(self, user_ids):
            raise RuntimeError("scrape_users falló")
        def scrape_connections(self, extra_profile_data, max_results):
            return pd.DataFrame()

    perfil, conexiones = scrape_profile_and_connections(FakeAccount(), "x", max_contacts=5)
    assert perfil["profile_id"] == "x"
    assert "scrape_error" in perfil
    assert conexiones.empty


def test_scrape_profile_and_connections_perfil_falla_conexiones_toomanyredirects():
    """Si el perfil falla y las conexiones lanzan TooManyRedirects, se devuelve perfil con error y conexiones vacías; account.on_block=True."""
    class FakeAccount:
        on_block = False
        def scrape_users(self, user_ids):
            raise ValueError("Failed to find user_id")
        def scrape_connections(self, extra_profile_data, max_results):
            raise requests.exceptions.TooManyRedirects("Exceeded 30 redirects")

    acc = FakeAccount()
    perfil, conexiones = scrape_profile_and_connections(acc, "x", max_contacts=5)
    assert perfil["profile_id"] == "x"
    assert "scrape_error" in perfil
    assert conexiones.empty
    assert acc.on_block is True
