#!/usr/bin/env python3
"""
Punto de entrada para ejecución programada (cron / systemd) en servidor.

Modos:
  --mode index   → Fase A: recopila slugs de conexiones y los encola.
  --mode enrich  → Fase B (defecto): enriquece contactos pendientes.

Cuenta:
  --account SLUG → cuenta a usar (carga sessions/{slug}.pkl).
                   Si se omite, usa la sesión por defecto (session.pkl).

Siempre corre en modo no interactivo (no abre navegador si la sesión caduca).

Delay aleatorio opcional (SCHEDULED_RANDOM_DELAY_MINUTES) para no golpear
LinkedIn siempre a la misma hora exacta.

── Ejemplos de cron ──────────────────────────────────────────────────────────

  # Reindexar domingos a las 9:00 (+ delay aleatorio de hasta 25 min)
  0 9 * * 0  cd /ruta && venv/bin/python run_scheduled.py --mode=index --account=miquel-roca

  # Enriquecer L-V a las 8h, 13h y 18h
  0 8,13,18 * * 1-5  cd /ruta && venv/bin/python run_scheduled.py --mode=enrich --account=miquel-roca

  # Segunda cuenta, escalonada 3 horas (11h, 16h, 21h)
  0 11,16,21 * * 1-5  cd /ruta && venv/bin/python run_scheduled.py --mode=enrich --account=otra-cuenta

  # Redirigir salida a logs del cron
  0 8 * * 1-5  cd /ruta && venv/bin/python run_scheduled.py --mode=enrich --account=miquel-roca >> logs/cron.log 2>&1
"""
import argparse
import os
import random
import time

# Forzar modo servidor: nunca abrir navegador interactivo
os.environ["LINKEDIN_NO_BROWSER"] = "1"

# Delay aleatorio antes de arrancar (evita patrones horarios predecibles)
_delay_min = int(os.environ.get("SCHEDULED_RANDOM_DELAY_MINUTES", "0"))
if _delay_min > 0:
    _delay_sec = random.randint(0, _delay_min * 60)
    print(f"[run_scheduled] Delay aleatorio: {_delay_sec}s antes de arrancar...")
    time.sleep(_delay_sec)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ejecutor programado del scraper de LinkedIn (sin interactividad)."
    )
    parser.add_argument(
        "--mode",
        choices=["index", "enrich", "legacy"],
        default="enrich",
        help=(
            "index  → recopilar slugs de conexiones (Fase A).\n"
            "enrich → enriquecer contactos pendientes (Fase B, defecto).\n"
            "legacy → flujo original perfil+CSV."
        ),
    )
    parser.add_argument(
        "--account", type=str, default=None, metavar="SLUG",
        help=(
            "Cuenta LinkedIn a usar (slug del perfil, ej. 'miquel-roca-mascaros'). "
            "Carga sessions/{slug}.pkl. Sin este argumento usa session.pkl."
        ),
    )
    parser.add_argument(
        "--max-contacts", type=int, default=None, metavar="N",
        help="Límite de contactos por ejecución (sobrescribe MAX_CONTACTS_PER_RUN).",
    )
    args = parser.parse_args()

    # Importar aquí para que el delay aleatorio ya haya ocurrido
    from main import run_index, run_enrich, run_scrape

    if args.mode == "index":
        run_index(interactive=False, account=args.account)
    elif args.mode == "enrich":
        run_enrich(
            interactive=False,
            max_contacts_override=args.max_contacts,
            account=args.account,
        )
    else:
        run_scrape(
            interactive=False,
            max_contacts_override=args.max_contacts,
            account=args.account,
        )


if __name__ == "__main__":
    main()
