# db.py
# Creación de la base de datos y tabla runs para el historial de ejecuciones del scraper.
# El viewer (viewer_app.py) usa la misma DB_PATH para listar runs.

import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = os.environ.get("DB_PATH", str(DATA_DIR / "contacts.db"))

RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    contacts_scraped INTEGER NOT NULL DEFAULT 0,
    contacts_new INTEGER NOT NULL DEFAULT 0,
    contacts_updated INTEGER NOT NULL DEFAULT 0
);
"""


def ensure_runs_table() -> None:
    """Crea el directorio data/ y la tabla runs si no existen."""
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(RUNS_SCHEMA)
    conn.close()


def insert_run(
    username: str,
    started_at: str,
    finished_at: str,
    contacts_scraped: int = 0,
    contacts_new: int = 0,
    contacts_updated: int = 0,
) -> None:
    """Registra una ejecución del scraper en la tabla runs."""
    ensure_runs_table()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO runs (username, started_at, finished_at, contacts_scraped, contacts_new, contacts_updated)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (username, started_at, finished_at, contacts_scraped, contacts_new, contacts_updated),
    )
    conn.commit()
    conn.close()
