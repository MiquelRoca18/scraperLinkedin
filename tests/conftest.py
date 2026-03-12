"""
Fixtures compartidas: datos de ejemplo como los que devuelve StaffSpy / LinkedIn.
"""
import pandas as pd
import pytest

# Columnas exactas del CSV de conexiones (output/conexiones_*_*.csv).
# El scraper debe rellenar estas columnas para que el export coincida.
COLUMNAS_CSV_CONEXIONES = [
    "profile_id",
    "name",
    "first_name",
    "last_name",
    "position",
    "company",
    "location",
    "emails",
    "phones",
    "is_connection",
    "followers",
    "connections",
    "profile_link",
    "profile_photo",
    "premium",
    "creator",
    "open_to_work",
]


@pytest.fixture
def staff_row_completo():
    """Una fila típica como StaffSpy (DataFrame) con todos los campos."""
    return pd.Series({
        "profile_id": "juan-garcia-123",
        "id": "ACoAAAxyz",
        "name": "Juan García",
        "first_name": "Juan",
        "last_name": "García",
        "position": "Software Engineer",
        "company": "Tech SA",
        "location": "Barcelona, Spain",
        "connection_email": "juan@empresa.com",
        "email_address": None,
        "potential_email": "juan.garcia@tech.com",
        "connection_phone_numbers": None,
        "phone_numbers": "['+34600123456']",
        "is_connection": "yes",
        "followers": 500,
        "connections": 500,
        "profile_link": "https://www.linkedin.com/in/juan-garcia-123",
        "profile_photo": "https://media.licdn.com/...",
        "premium": False,
        "creator": False,
        "open_to_work": True,
    })


@pytest.fixture
def staff_row_minimo():
    """Mínimo que puede devolver StaffSpy (solo campos básicos)."""
    return pd.Series({
        "profile_id": "maria-lopez",
        "name": "María López",
        "profile_link": "https://www.linkedin.com/in/maria-lopez",
    })


@pytest.fixture
def staff_row_solo_emails_alternativos():
    """Email en email_address y potential_emails (lista separada por comas)."""
    return pd.Series({
        "profile_id": "ana-martin",
        "name": "Ana Martín",
        "email_address": "ana@mail.com",
        "potential_emails": "a.martin@empresa.com, ana.martin@empresa.com",
        "company": "Empresa",
        "profile_link": "https://www.linkedin.com/in/ana-martin",
    })


@pytest.fixture
def staff_row_como_csv_real():
    """
    Fila con la misma forma que las del CSV real de conexiones:
    connection_email -> emails, phone_numbers -> phones, name, first_name, last_name, location, etc.
    """
    return pd.Series({
        "profile_id": "arturo-garcía-serna-ruiz",
        "id": "ACoAAAz320ABqTnY6NPSd1Nrut3FrhEiiybCc6c",
        "name": "Arturo García-Serna Ruiz",
        "first_name": "Arturo",
        "last_name": "García-Serna Ruiz",
        "position": None,
        "company": None,
        "location": "Dublin, County Dublin, Ireland",
        "connection_email": "agarciasernaruiz@linkedin.com",
        "email_address": None,
        "potential_email": None,
        "connection_phone_numbers": "['+353(86)2148158']",
        "phone_numbers": None,
        "is_connection": "yes",
        "followers": 16210,
        "connections": 15269,
        "profile_link": "https://www.linkedin.com/in/arturo-garcía-serna-ruiz",
        "profile_photo": "https://media.licdn.com/...",
        "premium": True,
        "creator": True,
        "open_to_work": False,
    })
