"""
Tests del módulo db: tabla runs y insert_run.
Comprueba que el esquema coincide con lo que espera viewer_app (api/runs).
"""
import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

import db as db_module


def test_ensure_runs_table_crea_tabla_y_insert_run():
    """ensure_runs_table crea la tabla; insert_run inserta una fila con el esquema esperado por el viewer."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "contacts.db")
        with patch.object(db_module, "DB_PATH", db_path):
            db_module.ensure_runs_table()
            db_module.insert_run(
            username="test-user",
            started_at="2025-01-15T10:00:00Z",
            finished_at="2025-01-15T10:05:00Z",
            contacts_scraped=10,
                contacts_new=0,
                contacts_updated=0,
            )

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, username, started_at, finished_at, contacts_scraped, contacts_new, contacts_updated FROM runs"
        ).fetchall()
        conn.close()

        assert len(rows) == 1
        r = dict(rows[0])
        assert r["username"] == "test-user"
        assert r["started_at"] == "2025-01-15T10:00:00Z"
        assert r["finished_at"] == "2025-01-15T10:05:00Z"
        assert r["contacts_scraped"] == 10
        assert r["contacts_new"] == 0
        assert r["contacts_updated"] == 0
