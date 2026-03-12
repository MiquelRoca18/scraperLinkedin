"""
Tests del flujo de main: cooldown, intervalo mínimo, extract_username,
get_username_non_interactive, run_scrape.
Sin llamadas a LinkedIn; se usan archivos temporales y mocks.
"""
import os
import sys
import time
import tempfile
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# Importar main después de preparar env si hace falta
import main as main_module


def test_extract_username_ok():
    assert main_module.extract_username("https://www.linkedin.com/in/juan-perez") == "juan-perez"
    assert main_module.extract_username("https://linkedin.com/in/maria-lopez-123") == "maria-lopez-123"


def test_extract_username_con_trailing_slash():
    assert main_module.extract_username("https://www.linkedin.com/in/juan-perez/") == "juan-perez"


def test_extract_username_con_query():
    assert main_module.extract_username("https://www.linkedin.com/in/juan-perez?trk=foo") == "juan-perez"


def test_extract_username_invalida():
    with pytest.raises(ValueError) as exc_info:
        main_module.extract_username("https://google.com/foo")
    assert "URL inválida" in str(exc_info.value)


# ----- Cooldown (429 / TooManyRedirects) -----


def test_check_cooldown_sin_archivo():
    with tempfile.TemporaryDirectory() as tmp:
        cooldown_file = os.path.join(tmp, "cooldown.txt")
        assert not os.path.isfile(cooldown_file)
        # Parchear para usar nuestro archivo (que no existe)
        main_module.COOLDOWN_FILE = cooldown_file
        assert main_module._check_cooldown() is False


def test_check_cooldown_archivo_futuro_activa_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        cooldown_file = os.path.join(tmp, "cooldown.txt")
        until = time.time() + 3600  # dentro de 1 hora
        with open(cooldown_file, "w") as f:
            f.write(str(until))
        main_module.COOLDOWN_FILE = cooldown_file
        assert main_module._check_cooldown() is True


def test_check_cooldown_archivo_pasado_permite_ejecutar_y_borra():
    with tempfile.TemporaryDirectory() as tmp:
        cooldown_file = os.path.join(tmp, "cooldown.txt")
        until = time.time() - 10  # hace 10 segundos
        with open(cooldown_file, "w") as f:
            f.write(str(until))
        main_module.COOLDOWN_FILE = cooldown_file
        assert main_module._check_cooldown() is False
        assert not os.path.isfile(cooldown_file)


def test_check_cooldown_archivo_corrupto_permite_ejecutar():
    with tempfile.TemporaryDirectory() as tmp:
        cooldown_file = os.path.join(tmp, "cooldown.txt")
        with open(cooldown_file, "w") as f:
            f.write("not a number")
        main_module.COOLDOWN_FILE = cooldown_file
        assert main_module._check_cooldown() is False


def test_write_cooldown_crea_archivo_con_timestamp_futuro():
    with tempfile.TemporaryDirectory() as tmp:
        cooldown_file = os.path.join(tmp, "cooldown.txt")
        main_module.COOLDOWN_FILE = cooldown_file
        main_module._write_cooldown()
        assert os.path.isfile(cooldown_file)
        with open(cooldown_file) as f:
            until = float(f.read().strip())
        assert until > time.time()


# ----- MIN_HOURS_BETWEEN_RUNS -----


def test_check_min_interval_desactivado():
    original = main_module.MIN_HOURS_BETWEEN_RUNS
    main_module.MIN_HOURS_BETWEEN_RUNS = 0
    try:
        assert main_module._check_min_interval() is False
    finally:
        main_module.MIN_HOURS_BETWEEN_RUNS = original


def test_check_min_interval_sin_archivo_permite_y_escribe():
    with tempfile.TemporaryDirectory() as tmp:
        last_run_file = os.path.join(tmp, "last_run.txt")
        main_module.LAST_RUN_FILE = last_run_file
        main_module.MIN_HOURS_BETWEEN_RUNS = 24
        assert not os.path.isfile(last_run_file)
        # Primera ejecución: permite y escribe
        assert main_module._check_min_interval() is False
        assert os.path.isfile(last_run_file)
        with open(last_run_file) as f:
            t = float(f.read().strip())
        assert t <= time.time() + 1 and t >= time.time() - 2


def test_check_min_interval_archivo_reciente_bloquea():
    with tempfile.TemporaryDirectory() as tmp:
        last_run_file = os.path.join(tmp, "last_run.txt")
        main_module.LAST_RUN_FILE = last_run_file
        main_module.MIN_HOURS_BETWEEN_RUNS = 24
        with open(last_run_file, "w") as f:
            f.write(str(time.time()))  # ahora mismo
        assert main_module._check_min_interval() is True


def test_check_min_interval_archivo_antiguo_permite_y_actualiza():
    with tempfile.TemporaryDirectory() as tmp:
        last_run_file = os.path.join(tmp, "last_run.txt")
        main_module.LAST_RUN_FILE = last_run_file
        main_module.MIN_HOURS_BETWEEN_RUNS = 1  # 1 hora
        old_ts = time.time() - 7200  # hace 2 horas
        with open(last_run_file, "w") as f:
            f.write(str(old_ts))
        assert main_module._check_min_interval() is False
        with open(last_run_file) as f:
            new_ts = float(f.read().strip())
        assert new_ts >= time.time() - 2


def test_check_min_interval_archivo_corrupto_permite():
    with tempfile.TemporaryDirectory() as tmp:
        last_run_file = os.path.join(tmp, "last_run.txt")
        main_module.LAST_RUN_FILE = last_run_file
        main_module.MIN_HOURS_BETWEEN_RUNS = 24
        with open(last_run_file, "w") as f:
            f.write("invalid")
        # Debería tratar como "no hay último run" o pasado: permite
        assert main_module._check_min_interval() is False


# ----- get_username_non_interactive -----


def test_get_username_non_interactive_desde_api():
    """Si get_current_username devuelve usuario, se usa ese."""
    fake = MagicMock()
    with patch("main.get_current_username", return_value="juan-perez"):
        assert main_module.get_username_non_interactive(fake) == "juan-perez"


def test_get_username_non_interactive_desde_env():
    """Si la API no devuelve usuario pero LINKEDIN_PROFILE_URL está en .env, se extrae."""
    fake = MagicMock()
    with patch("main.get_current_username", return_value=None):
        with patch.dict(os.environ, {"LINKEDIN_PROFILE_URL": "https://linkedin.com/in/maria-lopez"}):
            assert main_module.get_username_non_interactive(fake) == "maria-lopez"


def test_get_username_non_interactive_sin_usuario_ni_env_lanza():
    """Si no hay usuario ni LINKEDIN_PROFILE_URL, lanza ValueError."""
    fake = MagicMock()
    with patch("main.get_current_username", return_value=None):
        with patch.dict(os.environ, {}, clear=False):
            if "LINKEDIN_PROFILE_URL" in os.environ:
                del os.environ["LINKEDIN_PROFILE_URL"]
        with pytest.raises(ValueError) as exc_info:
            main_module.get_username_non_interactive(fake)
        assert "no interactivo" in str(exc_info.value).lower() or "LINKEDIN_PROFILE_URL" in str(exc_info.value)


# ----- run_scrape (mocks, sin red) -----


def test_run_scrape_no_interactivo_registra_run_y_escribe_csv():
    """run_scrape(interactive=False) con mocks: insert_run llamado y CSVs creados."""
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = os.path.join(tmp, "output")
        os.makedirs(out_dir, exist_ok=True)
        cooldown_f = os.path.join(tmp, "cooldown")
        last_run_f = os.path.join(tmp, "last_run")
        main_module.COOLDOWN_FILE = cooldown_f
        main_module.LAST_RUN_FILE = last_run_f
        main_module.MIN_HOURS_BETWEEN_RUNS = 0

        fake_account = MagicMock()
        fake_account.on_block = False
        perfil = {"profile_id": "test-user", "name": "Test"}
        conexiones = pd.DataFrame([{"profile_id": "c1", "name": "Conexión 1"}])

        with patch("main.init_client", return_value=fake_account):
            with patch("main.get_username_non_interactive", return_value="test-user"):
                with patch("main.scrape_profile_and_connections", return_value=(perfil, conexiones)):
                    with patch("main.insert_run") as mock_insert:
                        # run_scrape escribe en "output" relativo al cwd; redirigir cwd a tmp
                        orig_cwd = os.getcwd()
                        try:
                            os.chdir(tmp)
                            main_module.run_scrape(interactive=False)
                        finally:
                            os.chdir(orig_cwd)

        mock_insert.assert_called_once()
        call_kw = mock_insert.call_args[1]
        assert call_kw["username"] == "test-user"
        assert "started_at" in call_kw and "finished_at" in call_kw
        assert call_kw["contacts_scraped"] == 1
        assert call_kw["contacts_new"] == 0
        assert call_kw["contacts_updated"] == 0

        # Debe haber creado CSVs en tmp/output
        assert os.path.isdir(os.path.join(tmp, "output"))
        csvs = [f for f in os.listdir(os.path.join(tmp, "output")) if f.endswith(".csv")]
        assert len(csvs) >= 1
        assert any("perfil_" in f for f in csvs)
        assert any("conexiones_" in f for f in csvs)


def test_run_scrape_dry_run_no_llama_init_client():
    """Con dry_run=True no se llama a init_client ni scrape; solo comprueba cooldown/intervalo."""
    with tempfile.TemporaryDirectory() as tmp:
        main_module.COOLDOWN_FILE = os.path.join(tmp, "cooldown")
        main_module.LAST_RUN_FILE = os.path.join(tmp, "last_run")
        main_module.MIN_HOURS_BETWEEN_RUNS = 0
        with patch("main.init_client") as mock_init:
            with patch("main.scrape_profile_and_connections"):
                main_module.run_scrape(interactive=False, dry_run=True)
        mock_init.assert_not_called()
