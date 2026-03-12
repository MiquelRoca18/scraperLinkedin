# Scraper LinkedIn Personal

Script en Python para extraer datos de perfiles de LinkedIn (nombre, posición, empresa, ubicación, email, teléfono) usando [StaffSpy](https://github.com/cullenwatson/StaffSpy) y sesión guardada.

## Modos

1. **Conexiones de mi cuenta**: perfil del usuario logueado + lista de contactos con emails/teléfonos (cuando están disponibles).
2. **Perfil por URL**: extrae los mismos campos de un perfil dado su URL (email/teléfono solo si es tu conexión). Si se usa el navegador para cargar la página, además se extraen **conexiones en común** y **personas que quizá conozcas** (los bloques que LinkedIn muestra en el lateral) y se guardan en un CSV aparte.

## Cómo funciona el modo “Perfil por URL”

1. Se intenta obtener el perfil por API (profileView). Si LinkedIn devuelve 410 u otro error, se pasa al siguiente paso.
2. Se intenta obtener datos básicos del HTML con la sesión (requests). Si no hay suficiente información, se usa el navegador.
3. **Navegador**: se abre Chrome (o Firefox), se inyectan las cookies de tu sesión, se carga la URL del perfil y se recarga la página para evitar “error al cargar”. Del HTML ya renderizado se extrae:
   - Nombre, headline, empresa, ubicación (JSON-LD o DOM).
   - El **ID interno** del perfil (para poder pedir email/teléfono si es tu conexión).
4. Con ese ID se llama al endpoint de **contact info** de LinkedIn; solo devuelve email/teléfono si esa persona es **tu conexión de 1º**.
5. En la **misma página** del perfil se buscan enlaces a otros perfiles en los bloques de “conexiones en común” y “personas que quizá conozcas”. Se extraen `profile_id`, nombre (si aparece), URL y si viene de “mutual” o “pymk”. Esa lista se guarda en `output/sugeridos_url_<usuario>_<timestamp>.csv`.

LinkedIn **no** permite ver la lista completa de contactos de otro usuario; solo lo que muestra en esa página (un subconjunto de conexiones en común y sugerencias).

## Requisitos

- Python 3.10+
- Chrome (para el fallback por navegador en modo 2)

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Copia las variables de entorno en un archivo `.env` (no se sube al repo):

- `MAX_CONTACTS`: límite de contactos a scrapear (modo 1).
- `SLEEP_TIME`: pausa entre acciones (segundos).
- `LOG_LEVEL`: nivel de log de StaffSpy (0=errores, 1=info, 2=debug).
- `BROWSER_PROFILE_WAIT`: segundos de espera al cargar el perfil en el navegador (modo 2).
- `SLEEP_BETWEEN_REQUESTS` / `SLEEP_BETWEEN_CONNECTIONS`: pausas para no saturar a LinkedIn (por defecto 3 s y 6 s).
- `COOLDOWN_HOURS_AFTER_429`: horas sin hacer peticiones tras un 429 o redirecciones (por defecto 48).
- **`MIN_HOURS_BETWEEN_RUNS`**: mínimo de horas entre ejecuciones (por defecto 0 = desactivado). Si pones `24`, el script solo se podrá ejecutar una vez cada 24 h; útil en pruebas para no limitar la cuenta.

## Primera vez: iniciar sesión en LinkedIn

La **primera vez** que ejecutes el script no existirá el archivo `session.pkl`, así que el programa abrirá **automáticamente un navegador** (Chrome o Firefox):

1. Ejecuta `python main.py` y elige modo 1 o 2.
2. Cuando se abra el navegador, **inicia sesión en LinkedIn** con tu cuenta (email y contraseña).
3. Una vez dentro de LinkedIn, cierra el navegador o deja que el script continúe.
4. El script **creará automáticamente** el archivo `session.pkl` en la carpeta del proyecto con la sesión guardada.

A partir de la siguiente ejecución, el script usará `session.pkl` y **no volverá a pedir iniciar sesión** (salvo que caduque la sesión). **No borres `session.pkl` por costumbre**: mantenerlo evita iniciar sesión una y otra vez y reduce el riesgo de bloqueos. Solo bórralo si el script indica que la sesión ha caducado o si quieres usar otra cuenta de LinkedIn.

## Uso

```bash
python main.py
```

Elige el modo (1 o 2) y pega la URL del perfil. Los CSV se guardan en `output/`:

- **Modo 1**: `perfil_<usuario>_<timestamp>.csv`, `conexiones_<usuario>_<timestamp>.csv`
- **Modo 2**: `perfil_url_<usuario>_<timestamp>.csv` y, si el navegador encontró sugeridos, `sugeridos_url_<usuario>_<timestamp>.csv` (columnas: `profile_id`, `name`, `profile_link`, `source` donde `source` es `mutual`, `pymk` o `suggested`)

## Notas

- LinkedIn puede devolver 410 en algunos endpoints; el script usa fallback con navegador cuando hace falta.
- No subas `session.pkl` ni `.env` a ningún repositorio (ya están en `.gitignore`).

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
