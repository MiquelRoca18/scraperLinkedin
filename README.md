# Scraper LinkedIn Personal

Script en Python para extraer datos de perfiles de LinkedIn (nombre, posición, empresa, ubicación, email, teléfono) usando [StaffSpy](https://github.com/cullenwatson/StaffSpy) y sesión guardada.

## Qué hace el script ahora mismo

Solo hay **un modo principal**: conexiones de tu propia cuenta. Es decir:

- Obtiene el perfil de la cuenta que tiene la sesión iniciada.
- Descarga la lista de conexiones (hasta `MAX_CONTACTS`, con cap de seguridad).
- Intenta sacar, cuando LinkedIn lo permite, email/teléfono e información básica de cada conexión.

## Requisitos

- Python 3.10+

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Copia las variables de entorno en un archivo `.env` (no se sube al repo):

- `MAX_CONTACTS`: límite de contactos a scrapear (modo 1).
- `SLEEP_TIME`: pausa entre acciones (segundos).
- `LOG_LEVEL`: nivel de log de StaffSpy (0=errores, 1=info, 2=debug).
- `SLEEP_BETWEEN_REQUESTS` / `SLEEP_BETWEEN_CONNECTIONS`: pausas para no saturar a LinkedIn (por defecto 3 s y 6 s).
- `COOLDOWN_HOURS_AFTER_429`: horas sin hacer peticiones tras un 429 o redirecciones (por defecto 48).
- **`MIN_HOURS_BETWEEN_RUNS`**: mínimo de horas entre ejecuciones (por defecto 0 = desactivado). Si pones `24`, el script solo se podrá ejecutar una vez cada 24 h; útil en pruebas para no limitar la cuenta.
- **`LOG_DIR`** / **`LOG_FILE`**: directorio y archivo de log (por defecto `logs/scraper.log`).
- **`SCRAPER_LOG_LEVEL`**: nivel del log a archivo (INFO, DEBUG, WARNING, ERROR).

## Primera vez: iniciar sesión en LinkedIn

La **primera vez** que ejecutes el script no existirá el archivo `session.pkl`, así que el programa abrirá **automáticamente un navegador** (Chrome o Firefox):

1. Ejecuta `python main.py`.
2. Cuando se abra el navegador, **inicia sesión en LinkedIn** con tu cuenta (email y contraseña).
3. Una vez dentro de LinkedIn, cierra el navegador o deja que el script continúe.
4. El script **creará automáticamente** el archivo `session.pkl` en la carpeta del proyecto con la sesión guardada.

A partir de la siguiente ejecución, el script usará `session.pkl` y **no volverá a pedir iniciar sesión** (salvo que caduque la sesión). **No borres `session.pkl` por costumbre**: mantenerlo evita iniciar sesión una y otra vez y reduce el riesgo de bloqueos. Solo bórralo si el script indica que la sesión ha caducado o si quieres usar otra cuenta de LinkedIn.

### Sesión caducada y re-login automático

- **Modo interactivo** (terminal con teclado): si se detecta que la sesión ha caducado, el script borra `session.pkl`, abre el navegador y te pide que inicies sesión de nuevo. Si tras pulsar Enter el login no se completa (p. ej. había un captcha sin resolver), el script muestra un aviso y te pide que completes la verificación en el navegador y vuelvas a ejecutar el script.
- **Modo no interactivo** (cron, servidor sin pantalla): si la sesión ha caducado, el script no intenta abrir el navegador; termina con un mensaje indicando que debes ejecutarlo manualmente en un entorno donde puedas iniciar sesión.

## Uso

```bash
python main.py
```

Opciones por línea de comandos:

- **`--max-contacts N`**: límite de conexiones para esta ejecución (sobrescribe `.env`).
- **`--dry-run`**: solo comprueba cooldown e intervalo mínimo; no conecta ni scrapea.
- **`--no-browser`**: si la sesión ha caducado, no abre el navegador; termina con error (útil en cron para no colgar).

Ejemplos:

```bash
python main.py --dry-run
python main.py --max-contacts 10
python main.py --no-browser
```

El script detecta automáticamente tu usuario y guarda CSVs en `output/`. Los logs se escriben en `logs/scraper.log` (configurable con `LOG_DIR`, `LOG_FILE` y `SCRAPER_LOG_LEVEL` en `.env`).

- `perfil_<usuario>_<timestamp>.csv`
- `conexiones_<usuario>_<timestamp>.csv`

Cada ejecución se registra en la base de datos `data/contacts.db` (tabla `runs`). Así el **viewer** (`python viewer_app.py`) puede mostrar el historial de ejecuciones y lanzar un scrape desde el navegador (botón "Lanzar scrape"). En modo no interactivo (p. ej. desde el viewer) hace falta tener `LINKEDIN_PROFILE_URL` en `.env` o que la sesión devuelva el usuario automáticamente. La ruta de la DB se puede cambiar con la variable de entorno `DB_PATH`.

## Notas

- LinkedIn puede devolver 410 en algunos endpoints; el script usa fallback con navegador cuando hace falta.
- No subas `session.pkl` ni `.env` a ningún repositorio (ya están en `.gitignore`).
- Las peticiones HTTP tienen **reintentos** ante timeout o error de conexión (3 intentos con pausa de 1 s).
- Si una ejecución termina con **0 conexiones** y no hubo 429/redirecciones, se escribe un aviso en log (posible sesión inválida o cambio en LinkedIn).

### Tests (sin tocar LinkedIn)

Puedes comprobar que la lógica del scraper y los controles de cooldown/intervalo funcionan como en producción, **sin hacer ninguna petición a LinkedIn**.

**Usa el venv del proyecto** (ahí está instalado `staffspy`); si usas el Python del sistema, fallará `ModuleNotFoundError: No module named 'staffspy'`:

```bash
# Opción 1: con el Python del venv
./venv/bin/python -m pytest tests/ -v

# Opción 2: activar el venv y luego pytest
source venv/bin/activate   # en Windows: venv\Scripts\activate
pip install pytest
pytest tests/ -v
```

Los tests simulan respuestas de StaffSpy (perfil, conexiones, 429, TooManyRedirects, sesión caducada, etc.) y comprueban normalización, orquestador y límites. Así puedes corregir errores y validar antes de ejecutar contra LinkedIn.

### Frecuencia recomendada (evitar límites / bloqueos)

- **En pruebas**: pon en `.env` `MIN_HOURS_BETWEEN_RUNS=24` (o `12` si quieres probar más a menudo con pocos contactos). Así el script solo se ejecutará como máximo una vez cada 24 h (o 12 h) y no tendrás que acordarte.
- **Uso normal**: una ejecución al día o unas pocas por semana es razonable. Si has tenido 429 o redirecciones, espera 24–48 h antes de volver a ejecutar (el cooldown lo hace automático).

### Despliegue en servidor (nube)

Para subir el scraper a un servidor y **comprobar que todo funciona antes de desplegarlo**, ver **[DEPLOY.md](DEPLOY.md)**. Incluye: qué está listo para la nube, plan del scraper en el servidor, limitaciones anti-bloqueo, y 4 pruebas que puedes hacer en local para simular el servidor.
