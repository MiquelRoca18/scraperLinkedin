#!/usr/bin/env python3
"""
Punto de entrada para ejecución programada (cron / systemd) en servidor.
- No abre navegador si la sesión ha caducado (falla y escribe en log).
- Opcional: delay aleatorio para no ejecutar siempre a la misma hora (SCHEDULED_RANDOM_DELAY_MINUTES).

Uso en cron (ejemplo, una vez al día a las 9:00):
  0 9 * * * cd /ruta/al/proyecto && ./venv/bin/python run_scheduled.py >> logs/cron.log 2>&1
"""
import os
import random
import time

# Forzar modo servidor: no abrir navegador si la sesión caduca
os.environ["LINKEDIN_NO_BROWSER"] = "1"

# Delay aleatorio opcional (minutos) para no golpear LinkedIn siempre a la misma hora
delay_min = int(os.environ.get("SCHEDULED_RANDOM_DELAY_MINUTES", "0"))
if delay_min > 0:
    delay_sec = random.randint(0, delay_min * 60)
    time.sleep(delay_sec)

from main import run_scrape

if __name__ == "__main__":
    run_scrape(interactive=False)
