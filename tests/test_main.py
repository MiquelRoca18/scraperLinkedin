"""
Tests del flujo de main: cooldown, intervalo mínimo, extract_username.
Sin llamadas a LinkedIn; se usan archivos temporales.
"""
import os
import sys
import time
import tempfile
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
