# Despliegue en servidor (nube)

Guía para subir el scraper a un servidor y comprobar que todo funciona **antes** de desplegarlo.

---

## 1. ¿Qué está listo para el servidor?

| Aspecto | Estado |
|--------|--------|
| **Ejecución sin teclado** | ✅ `run_scrape(interactive=False)` + usuario por API o `LINKEDIN_PROFILE_URL` |
| **Sin abrir navegador en servidor** | ✅ `--no-browser` / `LINKEDIN_NO_BROWSER=1`: si la sesión caduca, termina con error y no intenta abrir navegador |
| **Límites anti-bloqueo** | ✅ Cooldown 48 h tras 429, intervalo mínimo opcional, throttling, tope de contactos |
| **Logs en archivo** | ✅ `logs/scraper.log` (útil en servidor sin consola) |
| **Historial de ejecuciones** | ✅ Tabla `runs` en SQLite; el viewer muestra historial |
| **Viewer en servidor** | ✅ Flask con trigger de scrape en segundo plano; puede ir detrás de un reverse proxy |
| **Ejecución programada** | ✅ Script `run_scheduled.py` para cron (una vez al día; opcional `SCHEDULED_RANDOM_DELAY_MINUTES` para no ejecutar siempre a la misma hora) |

---

## 2. Plan del scraper en el servidor

- **Viewer** (`python viewer_app.py`): se deja corriendo (systemd, screen, etc.). Sirve la web para ver CSVs, historial de runs y lanzar un scrape a mano.
- **Scrape automático**: un **cron** (o systemd timer) ejecuta `run_scheduled.py` **una vez al día** (o cada X horas si lo configuras). El script usa `--no-browser` y las mismas limitaciones (cooldown, intervalo mínimo, `MAX_CONTACTS`).
- **Sesión**: en el servidor **no hay navegador**. La primera vez (y cuando caduque la sesión) tendrás que generar `session.pkl` en tu **máquina local** (donde sí hay navegador), subir el archivo al servidor y reiniciar. Si la sesión caduca, el scraper fallará y en logs/viewer verás el error; entonces repites el proceso (login local → subir `session.pkl`).

Limitaciones que ya aplican y ayudan a evitar bloqueos:

- Cooldown de 48 h tras 429 o redirecciones en bucle.
- Opcional: `MIN_HOURS_BETWEEN_RUNS=24` para no ejecutar más de una vez al día.
- Throttling entre peticiones y entre conexiones.
- Tope de contactos con `MAX_CONTACTS` / `MAX_CONTACTS_CAP`.

---

## 3. Comprobar que el servidor funciona **antes** de subir a la nube

Prueba en tu máquina como si fuera el servidor (sin abrir navegador, mismo flujo que en la nube).

### 3.1 Preparar entorno “servidor” en local

1. **Sesión válida**: tener ya un `session.pkl` generado (ejecutando `python main.py` una vez en local y cerrando cuando termine).
2. **Usuario sin teclado**: en `.env` pon `LINKEDIN_PROFILE_URL=https://www.linkedin.com/in/TU-USUARIO` (tu perfil).
3. **Limitar contacto**: por ejemplo `MAX_CONTACTS=5` y `MIN_HOURS_BETWEEN_RUNS=0` para pruebas rápidas.

### 3.2 Prueba 1: Scrape en modo “servidor” (sin navegador)

```bash
# Simula lo que hará el cron en la nube
LINKEDIN_NO_BROWSER=1 python main.py --no-browser --max-contacts 3
```

- Si la sesión es válida: debe terminar guardando CSVs en `output/` y un registro en `data/contacts.db` (tabla `runs`).
- Si falla (por ejemplo sesión caducada): debe salir con mensaje claro y **sin** abrir el navegador.

### 3.3 Prueba 2: Dry-run (solo comprobar cooldown/intervalo)

```bash
python main.py --dry-run
```

Debe imprimir que cooldown e intervalo están OK y terminar sin conectar a LinkedIn.

### 3.4 Prueba 3: Viewer + trigger desde la web

1. Arranca el viewer:
   ```bash
   python viewer_app.py
   ```
2. Abre `http://localhost:5001` (y el token si tienes `VIEWER_SECRET`).
3. Pulsa “Lanzar scrape” (o el botón equivalente).
4. Comprueba en la misma página el estado (en ejecución / último error).
5. Cuando termine, revisa que en “Historial” o listado de runs aparezca la nueva ejecución y que en “Archivos” / `output/` estén los CSV.

### 3.5 Prueba 4: Ejecución programada (run_scheduled.py)

```bash
python run_scheduled.py
```

Debe comportarse igual que `python main.py --no-browser` (mismo límite de contactos, mismo uso de `session.pkl` y `.env`). Comprueba de nuevo `output/` y la tabla `runs`.

Si todas estas pruebas pasan en local, el mismo flujo debería funcionar en la nube.

---

## 4. Subir a la nube: qué llevar y qué configurar

- **Código**: clonar o copiar el repo (incluyendo `main.py`, `run_scheduled.py`, `viewer_app.py`, `scraper.py`, `db.py`, `log_config.py`, `requirements.txt`, etc.).
- **Entorno**: `python -m venv venv`, `pip install -r requirements.txt`.
- **Archivos que no se suben al repo** (subirlos por canal seguro o crearlos en el servidor):
  - **`.env`**: con al menos `LINKEDIN_PROFILE_URL`, y si quieres `MAX_CONTACTS`, `MIN_HOURS_BETWEEN_RUNS`, `COOLDOWN_HOURS_AFTER_429`, `VIEWER_SECRET`, `DB_PATH`, `LOG_FILE`, etc.
  - **`session.pkl`**: generado en local (login con navegador), luego subido al servidor en la carpeta del proyecto.
- **Carpetas**: el servidor creará `output/`, `data/`, `logs/` al ejecutar (o las puedes crear tú).
- **Viewer**: arrancar con `python viewer_app.py` (o con gunicorn/uWSGI si lo usas). Opcional: poner detrás de nginx con HTTPS y, si quieres, proteger con `VIEWER_SECRET`.
- **Cron** (ejemplo una vez al día a las 9:00):
  ```cron
  0 9 * * * cd /ruta/al/proyecto && ./venv/bin/python run_scheduled.py >> logs/cron.log 2>&1
  ```
  Opcional: en `.env` pon `SCHEDULED_RANDOM_DELAY_MINUTES=60` para que, al arrancar, espere 0–60 minutos al azar antes de scrapear (así no cae siempre a la misma hora). Si usas `MIN_HOURS_BETWEEN_RUNS`, el script no hará nada si la última ejecución fue hace menos de X horas.

---

## 5. Resumen

- **Sí**: Las implementaciones actuales permiten usarlo en un servidor (sin teclado, sin navegador, con límites y logs).
- **Plan en servidor**: Viewer siempre corriendo; cron (o similar) ejecutando `run_scheduled.py` con moderación (p. ej. una vez al día); sesión mantenida con `session.pkl` subido desde local cuando haga falta.
- **Limitaciones**: Cooldown, intervalo mínimo, throttling y tope de contactos ya están aplicados.
- **Comprobar antes de subir**: Haz en local las 4 pruebas de la sección 3 (scrape con `--no-browser`, dry-run, viewer + trigger, `run_scheduled.py`). Si todo va bien ahí, el servidor en la nube debería comportarse igual.
