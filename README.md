# Scraper LinkedIn Personal

Script en Python para extraer datos de perfiles de LinkedIn (nombre, posición, empresa, ubicación, email, teléfono) usando [StaffSpy](https://github.com/cullenwatson/StaffSpy) y sesión guardada.

## Modos

1. **Conexiones de mi cuenta**: perfil del usuario logueado + lista de contactos con emails/teléfonos (cuando están disponibles).
2. **Perfil por URL**: extrae los mismos campos de un perfil dado su URL (email/teléfono solo si es tu conexión).

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

La primera vez que ejecutes el script se abrirá el navegador para iniciar sesión en LinkedIn; la sesión se guarda en `session.pkl` para siguientes ejecuciones.

## Uso

```bash
python main.py
```

Elige el modo (1 o 2) y pega la URL del perfil. Los CSV se guardan en `output/`.

## Notas

- LinkedIn puede devolver 410 en algunos endpoints; el script usa fallback con navegador cuando hace falta.
- No subas `session.pkl` ni `.env` a ningún repositorio (ya están en `.gitignore`).
