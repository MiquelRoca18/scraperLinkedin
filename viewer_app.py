"""
API y frontend del scraper de LinkedIn.

Endpoints de solo lectura (CSV, runs, estadísticas):
  GET  /api/files                     → lista de CSVs
  GET  /api/files/<name>              → contenido de un CSV (json/csv/xlsx)
  GET  /api/runs                      → historial de ejecuciones

Gestión de cuentas:
  GET  /api/accounts                  → lista de cuentas registradas con stats
  POST /api/accounts                  → añadir/registrar una cuenta (login interactivo)
  DELETE /api/accounts/<username>     → desactivar una cuenta
  GET  /api/accounts/<username>/stats → stats de cola y contactos de una cuenta

Scraping bajo demanda:
  POST /api/trigger-scrape            → lanza un scrape (mode, account, max_contacts)
  GET  /api/trigger-status            → estado del scrape en curso

Uso:
  python viewer_app.py
  → http://localhost:5001

Si defines VIEWER_SECRET en .env, la web pedirá ese valor para acceder.
"""

import os
import sqlite3
import threading
from io import BytesIO
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, send_file

load_dotenv()

app = Flask(__name__, static_folder="viewer", static_url_path="")
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
DATA_DIR = BASE_DIR / "data"
DB_PATH = os.environ.get("DB_PATH", str(DATA_DIR / "contacts.db"))
VIEWER_SECRET = os.environ.get("VIEWER_SECRET", "").strip()

# Estado del scrape bajo demanda (thread-safe)
_scrape_lock = threading.Lock()
_scrape_running = False
_scrape_last_error = None
_scrape_mode    = None
_scrape_account = None

# Estado de los logins en curso, uno por cuenta (thread-safe)
_login_lock: threading.Lock = threading.Lock()
_login_status: dict = {}  # { username: { "status": ..., "message": ... } }

# Control de cadencia de scrapes manuales (thread-safe)
# Evita que el usuario pulse el botón 10 veces seguidas y se bloquee la cuenta.
_last_trigger_lock = threading.Lock()
_last_trigger_time: dict = {}        # { "username:mode": epoch_seconds }
_MIN_ENRICH_INTERVAL = 20 * 60      # 20 min mínimo entre runs de enrich del mismo usuario
_MIN_INDEX_INTERVAL  = 60 * 60      # 60 min mínimo entre reindexaciones


# ── Auth ───────────────────────────────────────────────────────────────────────

def _check_auth() -> bool:
    if not VIEWER_SECRET:
        return True
    token = (
        request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        or request.args.get("token", "").strip()
    )
    return token == VIEWER_SECRET


def _safe_filename(name: str) -> bool:
    return name and "/" not in name and "\\" not in name and ".." not in name


def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Frontend ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ── CSV files ──────────────────────────────────────────────────────────────────

@app.route("/api/files")
def list_files():
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    if not OUTPUT_DIR.exists():
        return jsonify([])
    files = []
    for f in sorted(OUTPUT_DIR.glob("*.csv"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = f.stat()
        rows = 0
        try:
            with open(f, "r", encoding="utf-8-sig") as fp:
                rows = max(0, sum(1 for _ in fp) - 1)
        except Exception:
            pass
        files.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime, "rows": rows})
    return jsonify(files)


@app.route("/api/files/<path:name>")
def get_file(name):
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    if not _safe_filename(name):
        return jsonify({"error": "Nombre no válido"}), 400
    path = OUTPUT_DIR / name
    if not path.exists() or not path.is_file():
        return jsonify({"error": "No encontrado"}), 404
    fmt = request.args.get("format", "json")
    if fmt == "csv":
        return send_from_directory(OUTPUT_DIR, name, as_attachment=True, download_name=name)
    if fmt == "xlsx":
        try:
            df = pd.read_csv(path, encoding="utf-8-sig")
            buf = BytesIO()
            xlsx_name = name.replace(".csv", ".xlsx") if name.endswith(".csv") else name + ".xlsx"
            df.to_excel(buf, index=False, engine="openpyxl")
            buf.seek(0)
            return send_file(
                buf, as_attachment=True, download_name=xlsx_name,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
        df = df.fillna("")
        return jsonify(df.to_dict(orient="records"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Contactos (DB, paginado) ───────────────────────────────────────────────────

# Columnas que se devuelven al frontend para la tabla (excluye IDs y foto)
_CONTACT_DISPLAY_COLS = [
    "name", "first_name", "last_name",
    "position", "company", "location",
    "emails", "phones", "profile_link",
    "premium", "creator", "open_to_work",
    "followers", "connections",
    "first_scraped_at", "last_scraped_at",
]

# ── Mapas de exportación ──────────────────────────────────────────────────────
# Cada mapa es una lista de tuplas (columna_BD, nombre_cabecera).
# Permite exportar con nombres amigables según el destino.

# Formato completo en castellano — incluye todos los campos útiles
_EXPORT_COLS_FULL: list[tuple[str, str]] = [
    ("name",             "Nombre completo"),
    ("first_name",       "Nombre"),
    ("last_name",        "Apellidos"),
    ("position",         "Cargo"),
    ("company",          "Empresa"),
    ("location",         "Ubicación"),
    ("emails",           "Email"),
    ("phones",           "Teléfono"),
    ("profile_link",     "URL LinkedIn"),
    ("premium",          "Premium"),
    ("open_to_work",     "Disponible para trabajar"),
    ("followers",        "Seguidores"),
    ("connections",      "Conexiones"),
    ("first_scraped_at", "Primera vez scrapeado"),
    ("last_scraped_at",  "Último scraping"),
]

# Formato HubSpot — columnas mínimas, nombres en inglés que HubSpot importa
# directamente sin mapeo manual (First Name, Last Name, Email Address…)
_EXPORT_COLS_HUBSPOT: list[tuple[str, str]] = [
    ("first_name",   "First Name"),
    ("last_name",    "Last Name"),
    ("emails",       "Email Address"),
    ("phones",       "Phone Number"),
    ("company",      "Company Name"),
    ("position",     "Job Title"),
    ("location",     "City"),
    ("profile_link", "LinkedIn URL"),
    ("name",         "Full Name"),
]

# Formato Salesforce — nombres de campo estándar de Salesforce CRM
_EXPORT_COLS_SALESFORCE: list[tuple[str, str]] = [
    ("first_name",   "FirstName"),
    ("last_name",    "LastName"),
    ("emails",       "Email"),
    ("phones",       "Phone"),
    ("company",      "AccountName"),
    ("position",     "Title"),
    ("location",     "MailingCity"),
    ("profile_link", "LinkedIn_URL__c"),
    ("name",         "Name"),
]

_EXPORT_FORMATS: dict[str, list[tuple[str, str]]] = {
    "full":       _EXPORT_COLS_FULL,
    "hubspot":    _EXPORT_COLS_HUBSPOT,
    "salesforce": _EXPORT_COLS_SALESFORCE,
}


def _parse_contact_params():
    """Extrae y valida los query-params comunes de /api/contacts."""
    account     = request.args.get("account",     "").strip()
    page        = max(1, int(request.args.get("page",     1)))
    per_page    = min(max(1, int(request.args.get("per_page", 50))), 200)
    search      = request.args.get("search",      "").strip()
    filter_mode = request.args.get("filter",      "all").strip()
    sort_col    = request.args.get("sort",        "last_scraped_at").strip()
    sort_order  = request.args.get("order",       "desc").strip()
    run_from    = request.args.get("run_from",    "").strip() or None
    run_to      = request.args.get("run_to",      "").strip() or None
    if filter_mode not in ("all", "email", "phone", "email_phone"):
        filter_mode = "all"
    return account, page, per_page, search, filter_mode, sort_col, sort_order, run_from, run_to


@app.route("/api/contacts")
def list_contacts():
    """
    Devuelve una página de contactos desde la BD con filtros y paginación.

    Query params:
      account    → username de la cuenta (obligatorio)
      page       → número de página, empieza en 1 (default: 1)
      per_page   → filas por página, máx 200 (default: 50)
      search     → texto libre (busca en nombre, empresa, cargo, ubicación, email)
      filter     → "all" | "email" | "phone" | "email_phone"
      sort       → columna a ordenar (default: last_scraped_at)
      order      → "asc" | "desc" (default: desc)
      run_from   → ISO timestamp: solo contactos scrapeados desde esta fecha
      run_to     → ISO timestamp: solo contactos scrapeados hasta esta fecha

    Respuesta: { total, page, per_page, pages, contacts: [...] }
    """
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    try:
        account, page, per_page, search, filter_mode, sort_col, sort_order, run_from, run_to = (
            _parse_contact_params()
        )
        if not account:
            return jsonify({"error": "El parámetro 'account' es obligatorio"}), 400

        from db import get_contacts_paginated, count_contacts_filtered
        total    = count_contacts_filtered(account, search, filter_mode, run_from, run_to)
        contacts = get_contacts_paginated(
            account, page, per_page, search, filter_mode, sort_col, sort_order, run_from, run_to
        )

        # Proyectar solo las columnas de visualización
        def _project(c):
            return {k: c.get(k) for k in _CONTACT_DISPLAY_COLS if k in c}

        return jsonify({
            "total":    total,
            "page":     page,
            "per_page": per_page,
            "pages":    max(1, -(-total // per_page)),   # ceil division
            "contacts": [_project(c) for c in contacts],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _csv_stream_generator(
    account, search, filter_mode, sort_col, sort_order, run_from, run_to,
    max_rows: int = 0,
    col_map: "list[tuple[str,str]] | None" = None,
):
    """
    Generador que produce el CSV en lotes de 500 filas.
    Compatible con cualquier volumen; memoria prácticamente constante.

    col_map  → lista de (columna_BD, cabecera_CSV). Si es None usa _EXPORT_COLS_FULL.
    max_rows → 0 = sin límite; N = detiene tras N filas.
    """
    import csv
    import io as _io

    if col_map is None:
        col_map = _EXPORT_COLS_FULL

    db_cols  = [k for k, _ in col_map]
    hdr_cols = [h for _, h in col_map]

    BATCH   = 500
    page    = 1
    emitted = 0

    # BOM UTF-8 + fila de cabecera
    hdr_buf = _io.StringIO()
    csv.writer(hdr_buf).writerow(hdr_cols)
    yield ("\ufeff" + hdr_buf.getvalue()).encode("utf-8")

    from db import get_contacts_paginated
    while True:
        if max_rows and emitted >= max_rows:
            break
        batch_size = BATCH if not max_rows else min(BATCH, max_rows - emitted)
        batch = get_contacts_paginated(
            account, page=page, per_page=batch_size,
            search=search, filter_mode=filter_mode,
            sort_col=sort_col, sort_order=sort_order,
            run_from=run_from, run_to=run_to,
        )
        if not batch:
            break
        row_buf = _io.StringIO()
        w = csv.writer(row_buf)
        for c in batch:
            w.writerow([c.get(col) or "" for col in db_cols])
        yield row_buf.getvalue().encode("utf-8")
        emitted += len(batch)
        if len(batch) < batch_size:
            break
        page += 1


# Máximo de filas que se generan en un Excel (openpyxl es lento > 100k)
_XLSX_MAX_ROWS = 100_000


@app.route("/api/contacts/export")
def export_contacts():
    """
    Exporta contactos filtrados.

    Query params (además de los filtros de /api/contacts):
      format   → "csv" | "xlsx"
      crm      → "full" (completo ES, por defecto) | "hubspot" | "salesforce"
      max_rows → 0 = sin límite; N = exportar solo los primeros N

    CSV  → streaming, sin límite de filas, memoria constante.
    XLSX → generado en memoria, límite de _XLSX_MAX_ROWS filas.
    """
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401

    fmt = request.args.get("format", "csv").lower()
    if fmt not in ("csv", "xlsx"):
        return jsonify({"error": "Formato no soportado. Usa 'csv' o 'xlsx'"}), 400

    crm_key  = request.args.get("crm", "full").lower()
    col_map  = _EXPORT_FORMATS.get(crm_key, _EXPORT_COLS_FULL)
    max_rows = max(0, request.args.get("max_rows", 0, type=int))

    account, _, _, search, filter_mode, sort_col, sort_order, run_from, run_to = (
        _parse_contact_params()
    )
    if not account:
        return jsonify({"error": "El parámetro 'account' es obligatorio"}), 400

    # Sufijo del nombre de archivo: cuenta + crm (si no es full) + fecha de run (opcional)
    crm_tag = f"_{crm_key}" if crm_key != "full" else ""
    suffix  = f"_{account}{crm_tag}"
    if run_from:
        suffix += f"_{run_from[:10]}"

    if fmt == "csv":
        from flask import Response
        gen = _csv_stream_generator(
            account, search, filter_mode, sort_col, sort_order, run_from, run_to,
            max_rows=max_rows, col_map=col_map,
        )
        return Response(
            gen,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="contactos{suffix}.csv"'},
        )

    # ── Excel ──────────────────────────────────────────────────────────────────
    try:
        from db import get_contacts_paginated, count_contacts_filtered
        total = count_contacts_filtered(account, search, filter_mode, run_from, run_to)
        if not total:
            return jsonify({"error": "No hay contactos que coincidan con los filtros"}), 404

        xlsx_limit = _XLSX_MAX_ROWS if not max_rows else min(max_rows, _XLSX_MAX_ROWS)
        contacts   = get_contacts_paginated(
            account, page=1, per_page=xlsx_limit,
            search=search, filter_mode=filter_mode,
            sort_col=sort_col, sort_order=sort_order,
            run_from=run_from, run_to=run_to,
        )

        db_cols  = [k for k, _ in col_map]
        hdr_cols = [h for _, h in col_map]
        df = pd.DataFrame(
            [{h: c.get(k) or "" for k, h in col_map} for c in contacts],
            columns=hdr_cols,
        )

        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Contactos")
            exported = len(contacts)
            if exported < total:
                info_df = pd.DataFrame([{
                    "Información": (
                        f"Este archivo contiene {exported:,} de {total:,} contactos "
                        f"disponibles con los filtros actuales. "
                        f"Usa el botón CSV para exportar todos sin límite."
                    )
                }])
                info_df.to_excel(writer, index=False, sheet_name="Info")
        buf.seek(0)
        return send_file(
            buf, download_name=f"contactos{suffix}.xlsx",
            as_attachment=True,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Keepalive ──────────────────────────────────────────────────────────────────

@app.route("/ping")
def ping():
    """
    Endpoint público de keepalive — no requiere autenticación.
    Úsalo desde cron-job.org u otro servicio externo para evitar
    que el servidor se duerma en plataformas con free-tier.
    Devuelve 200 OK con un JSON mínimo.
    """
    from datetime import datetime, timezone
    return jsonify({"ok": True, "ts": datetime.now(timezone.utc).isoformat()})


# ── Runs ───────────────────────────────────────────────────────────────────────

@app.route("/api/runs")
def list_runs():
    """
    Devuelve el historial de ejecuciones.

    Query params:
      days    → mostrar solo los runs de los últimos N días (default: 30)
                Usa days=0 para mostrar todos sin límite temporal.
      limit   → máximo de runs a devolver (default: 200, máx: 500)
      account → filtrar por cuenta (opcional)
    """
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    if not Path(DB_PATH).exists():
        return jsonify([])
    try:
        days    = request.args.get("days",    30,  type=int)
        limit   = min(request.args.get("limit", 200, type=int), 500)
        account = request.args.get("account", "").strip()

        clauses = []
        params: list = []

        if days and days > 0:
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            clauses.append("started_at >= ?")
            params.append(cutoff)

        if account:
            clauses.append("username = ?")
            params.append(account)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        conn = _db_conn()
        rows = conn.execute(
            f"""SELECT id, username, started_at, finished_at,
                       contacts_scraped, contacts_new, contacts_updated
                FROM runs {where}
                ORDER BY id DESC LIMIT ?""",
            params,
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Accounts ───────────────────────────────────────────────────────────────────

def _account_stats(username: str) -> dict:
    """Estadísticas de cola y contactos para una cuenta."""
    try:
        from db import get_queue_stats, get_daily_count
        queue = get_queue_stats(username)
        daily = get_daily_count(username)
        contacts_total = 0
        if Path(DB_PATH).exists():
            conn = _db_conn()
            row = conn.execute(
                "SELECT COUNT(*) as n FROM contacts WHERE username = ?", (username,)
            ).fetchone()
            conn.close()
            contacts_total = row["n"] if row else 0
        return {
            "queue_pending": queue.get("pending", 0),
            "queue_done": queue.get("done", 0),
            "queue_error": queue.get("error", 0),
            "queue_total": queue.get("total", 0),
            "contacts_total": contacts_total,
            "daily_count": daily,
        }
    except Exception:
        return {}


def _session_status(session_file: str) -> dict:
    """
    Comprueba si el archivo de sesión existe y su antigüedad.
    No hace ninguna petición a LinkedIn — solo metadatos del archivo.
    """
    p = Path(session_file)
    if not p.exists():
        return {"session_exists": False, "session_age_days": None, "session_ok": False}
    try:
        import time as _time
        age_days = (_time.time() - p.stat().st_mtime) / 86400
        # Consideramos la sesión potencialmente válida si tiene menos de 75 días
        session_ok = age_days < 75
        return {
            "session_exists": True,
            "session_age_days": round(age_days, 1),
            "session_ok": session_ok,
        }
    except Exception:
        return {"session_exists": True, "session_age_days": None, "session_ok": None}


@app.route("/api/accounts", methods=["GET"])
def list_accounts():
    """Lista todas las cuentas activas con sus estadísticas de cola y estado de sesión."""
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    try:
        from db import list_accounts as db_list_accounts
        accounts = db_list_accounts(include_inactive=False)
        result = []
        for acc in accounts:
            stats = _account_stats(acc["username"])
            session_info = _session_status(acc.get("session_file", ""))
            # Ocultar credenciales del proxy — mostrar solo host:port
            proxy_raw = acc.get("proxy") or ""
            proxy_masked = proxy_raw.split("@")[-1] if "@" in proxy_raw else proxy_raw or None
            result.append({
                **acc,
                "proxy": proxy_masked,
                **stats,
                **session_info,
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts", methods=["POST"])
def add_account():
    """
    Registra una nueva cuenta e inicia sesión en LinkedIn en segundo plano.
    Body JSON:
      {
        "username":     "slug-de-linkedin",   (opcional — se auto-detecta tras el login)
        "email":        "correo@ejemplo.com",  (obligatorio)
        "password":     "contraseña",          (obligatorio)
        "display_name": "Nombre opcional",
        "proxy":        "user:pass@host:port"  (opcional)
      }
    La contraseña NO se almacena en la base de datos.
    """
    import re as _re
    import time as _time
    import shutil

    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401

    data = request.get_json(silent=True) or {}
    username     = (data.get("username")     or "").strip()
    email        = (data.get("email")        or "").strip()
    password     = (data.get("password")     or "").strip()
    display_name = (data.get("display_name") or "").strip()
    proxy        = (data.get("proxy")        or "").strip()

    if not email:
        return jsonify({"error": "El campo 'email' es obligatorio"}), 400
    if not password:
        return jsonify({"error": "El campo 'password' es obligatorio"}), 400
    if username and not _re.match(r"^[a-zA-Z0-9][a-zA-Z0-9\-_]{1,}$", username):
        return jsonify({"error": "El slug solo puede contener letras, números, guiones y guiones bajos"}), 400

    from scraper import session_file_for
    from db import register_account

    # Si el slug no se proporcionó, usamos un nombre temporal y lo renombramos
    # tras detectar el username real de LinkedIn.
    auto_detect = not username
    work_username = username if username else f"_tmp_{int(_time.time())}"

    session_path = session_file_for(work_username)

    # Registrar en la DB con nombre provisional (o definitivo si se proporcionó)
    register_account(work_username, session_path, display_name or work_username, proxy=proxy, email=email)

    # Si ya hay sesión válida (slug conocido + fichero existente), no hace falta login
    if not auto_detect and Path(session_path).exists():
        return jsonify({
            "status": "already_logged_in",
            "username": work_username,
            "message": f"La cuenta '{work_username}' ya tiene sesión activa.",
        })

    # Marcar como "login en curso"
    with _login_lock:
        _login_status[work_username] = {"status": "running", "message": "Iniciando sesión en LinkedIn…"}

    def _do_login():
        try:
            from scraper import login_with_credentials
            result = login_with_credentials(work_username, email, password, proxy=proxy or None, headless=True)

            # Si el login fue ok y no tenemos slug definitivo, renombramos
            if result.get("status") == "ok" and auto_detect:
                detected = result.get("detected_username") or ""
                if detected and _re.match(r"^[a-zA-Z0-9][a-zA-Z0-9\-_]{1,}$", detected):
                    real_session = session_file_for(detected)
                    tmp_session  = session_file_for(work_username)
                    # Mover fichero de sesión
                    if os.path.exists(tmp_session) and tmp_session != real_session:
                        os.makedirs(os.path.dirname(real_session), exist_ok=True)
                        shutil.move(tmp_session, real_session)
                    # Registrar con el nombre real y desactivar el temporal
                    from db import register_account as _reg, deactivate_account
                    _reg(detected, real_session, display_name or detected, proxy=proxy, email=email)
                    deactivate_account(work_username)
                    result["final_username"] = detected
                    # Guardar también bajo el nombre real para que el frontend lo pueda consultar
                    with _login_lock:
                        _login_status[detected] = result

            # Login OK: guardar contraseña cifrada para re-login automático futuro
            if result.get("status") == "ok":
                final_user = result.get("final_username") or work_username
                try:
                    from db import save_account_credentials
                    save_account_credentials(final_user, password)
                except Exception:
                    pass  # Si CREDENTIAL_KEY no está configurada, se ignora silenciosamente

        except Exception as exc:
            result = {"status": "error", "message": str(exc)}

        with _login_lock:
            _login_status[work_username] = result

    threading.Thread(target=_do_login, daemon=True).start()

    return jsonify({
        "status": "login_started",
        "poll_username": work_username,
        "message": "Login iniciado en segundo plano.",
    }), 202


@app.route("/api/accounts/<username>/login-status")
def account_login_status(username: str):
    """
    Estado del login en curso para una cuenta.
    Posibles valores de 'status':
      running            → el proceso de login está en ejecución
      ok                 → login completado, sesión guardada
      needs_verification → LinkedIn pide 2FA o captcha
      wrong_credentials  → email o contraseña incorrectos
      error              → error inesperado
      unknown            → no se ha iniciado ningún login para esta cuenta
    """
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    with _login_lock:
        info = _login_status.get(username, {"status": "unknown"})
    return jsonify({"username": username, **info})


@app.route("/api/accounts/<username>", methods=["DELETE"])
def remove_account(username: str):
    """Desactiva una cuenta (no borra datos históricos)."""
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    from db import deactivate_account
    found = deactivate_account(username)
    if not found:
        return jsonify({"error": f"Cuenta '{username}' no encontrada"}), 404
    with _login_lock:
        _login_status.pop(username, None)
    return jsonify({"status": "removed", "username": username})


@app.route("/api/accounts/<username>", methods=["PATCH"])
def edit_account(username: str):
    """
    Actualiza el proxy de una cuenta.
    Body JSON: { "proxy": "user:pass@host:port" }
    """
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    data  = request.get_json(silent=True) or {}
    proxy = (data.get("proxy") or "").strip()
    from db import update_account_proxy
    found = update_account_proxy(username, proxy)
    if not found:
        return jsonify({"error": f"Cuenta '{username}' no encontrada"}), 404
    proxy_masked = proxy.split("@")[-1] if "@" in proxy else proxy or None
    return jsonify({"status": "updated", "username": username, "proxy": proxy_masked})


@app.route("/api/accounts/<username>/stats")
def account_stats(username: str):
    """Stats detalladas de cola y contactos para una cuenta."""
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    stats = _account_stats(username)
    return jsonify({"username": username, **stats})


@app.route("/api/accounts/<username>/session-status")
def account_session_status(username: str):
    """
    Comprueba si la sesión de una cuenta existe y su antigüedad (días).
    No hace ninguna petición a LinkedIn.
    """
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    try:
        from db import list_accounts as db_list_accounts
        accounts = db_list_accounts(include_inactive=True)
        acc = next((a for a in accounts if a["username"] == username), None)
        if acc is None:
            return jsonify({"error": f"Cuenta '{username}' no encontrada"}), 404
        status = _session_status(acc.get("session_file", ""))
        return jsonify({"username": username, **status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Scraping bajo demanda ─────────────────────────────────────────────────────

def _run_scrape_background(
    mode: str, account: str | None, max_contacts: int | None, index_deep: bool = False
):
    global _scrape_running, _scrape_last_error, _scrape_mode, _scrape_account
    # Para reindexado profundo, fijar env ANTES de importar (scraper lee INDEX_* en tiempo de ejecución)
    old_index_max = old_index_rounds = None
    if mode == "index" and index_deep:
        old_index_max = os.environ.get("INDEX_MAX_CONTACTS")
        old_index_rounds = os.environ.get("INDEX_MAX_SCROLL_ROUNDS")
        os.environ["INDEX_MAX_CONTACTS"] = "500"
        os.environ["INDEX_MAX_SCROLL_ROUNDS"] = "80"
    try:
        os.environ["LINKEDIN_NO_BROWSER"] = "1"
        from main import run_index, run_enrich, run_scrape
        if mode == "index":
            run_index(interactive=False, account=account)
        elif mode == "enrich":
            run_enrich(interactive=False, max_contacts_override=max_contacts, account=account)
        else:
            run_scrape(interactive=False, max_contacts_override=max_contacts, account=account)
        with _scrape_lock:
            _scrape_last_error = None
    except Exception as e:
        with _scrape_lock:
            _scrape_last_error = str(e)
    finally:
        if mode == "index" and index_deep:
            if old_index_max is not None:
                os.environ["INDEX_MAX_CONTACTS"] = old_index_max
            elif "INDEX_MAX_CONTACTS" in os.environ:
                del os.environ["INDEX_MAX_CONTACTS"]
            if old_index_rounds is not None:
                os.environ["INDEX_MAX_SCROLL_ROUNDS"] = old_index_rounds
            elif "INDEX_MAX_SCROLL_ROUNDS" in os.environ:
                del os.environ["INDEX_MAX_SCROLL_ROUNDS"]
        with _scrape_lock:
            _scrape_running = False
            _scrape_mode    = None
            _scrape_account = None


@app.route("/api/accounts/<username>/contacts")
def account_contacts(username: str):
    """
    Devuelve los contactos enriquecidos de una cuenta desde la base de datos.
    Soporta ?format=json (defecto), csv, xlsx.
    Estos son los contactos guardados por el modo 'enrich', no los CSVs de legacy.
    """
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    fmt = request.args.get("format", "json").lower()
    try:
        from db import get_contacts
        contacts = get_contacts(username)
        if not contacts and fmt != "json":
            return jsonify({"error": "No hay contactos para esta cuenta"}), 404

        # Columnas relevantes para mostrar/exportar (excluir internas e IDs)
        EXPORT_COLS = [
            "name", "position", "company", "location",
            "emails", "phones", "profile_link",
            "premium", "creator", "open_to_work",
            "followers", "connections", "last_scraped_at",
        ]

        if fmt == "json":
            # Para el viewer devolvemos todo
            return jsonify(contacts)

        df = pd.DataFrame(contacts)
        if df.empty:
            return jsonify({"error": "No hay contactos para esta cuenta"}), 404
        cols = [c for c in EXPORT_COLS if c in df.columns]
        df = df[cols]

        q = f"?token={request.args.get('token','')}" if request.args.get("token") else ""
        if fmt == "csv":
            buf = BytesIO()
            df.to_csv(buf, index=False, encoding="utf-8-sig")
            buf.seek(0)
            return send_file(buf, download_name=f"contactos_{username}.csv",
                             as_attachment=True, mimetype="text/csv")
        if fmt == "xlsx":
            buf = BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="Contactos")
            buf.seek(0)
            return send_file(
                buf, download_name=f"contactos_{username}.xlsx", as_attachment=True,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        return jsonify({"error": f"Formato no soportado: {fmt}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trigger-scrape", methods=["POST"])
def trigger_scrape():
    """
    Lanza un scrape ahora (en segundo plano).
    Body JSON opcional:
      { "mode": "enrich"|"index"|"legacy",
        "account": "slug-de-linkedin",
        "max_contacts": 20,
        "deep": true }   (solo mode=index: reindexado profundo, hasta 500 slugs)

    Aplica un mínimo de tiempo entre ejecuciones para proteger la cuenta:
      - enrich: 20 minutos mínimo entre runs del mismo usuario
      - index:  60 minutos mínimo entre re-indexaciones del mismo usuario
    """
    import time as _time
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401

    body        = request.get_json(silent=True) or {}
    mode        = body.get("mode", "legacy")
    account     = body.get("account") or None
    max_contacts = body.get("max_contacts") or None
    index_deep  = bool(body.get("deep")) and mode == "index"

    if mode not in ("index", "enrich", "legacy"):
        return jsonify({"error": f"Modo no válido: {mode}"}), 400

    # ── Rate limiting ─────────────────────────────────────────────────────────
    min_interval = {
        "enrich": _MIN_ENRICH_INTERVAL,
        "index":  _MIN_INDEX_INTERVAL,
    }.get(mode, 0)

    if min_interval:
        key  = f"{account or '_default'}:{mode}"
        now  = _time.time()
        with _last_trigger_lock:
            last = _last_trigger_time.get(key, 0)
            elapsed = now - last
            if elapsed < min_interval:
                remaining_min = int((min_interval - elapsed) / 60) + 1
                return jsonify({
                    "error": (
                        f"Demasiado pronto. Han pasado solo {int(elapsed / 60)} min desde el "
                        f"último {mode}. Espera {remaining_min} min más para proteger la cuenta."
                    ),
                    "cooldown_remaining_minutes": remaining_min,
                    "rate_limited": True,
                }), 429
            _last_trigger_time[key] = now
    # ─────────────────────────────────────────────────────────────────────────

    global _scrape_running, _scrape_mode, _scrape_account
    with _scrape_lock:
        if _scrape_running:
            return jsonify({"error": "Ya hay un scrape en ejecución", "running": True}), 409
        _scrape_running = True
        _scrape_mode    = mode
        _scrape_account = account

    thread = threading.Thread(
        target=_run_scrape_background,
        args=(mode, account, max_contacts, index_deep),
        daemon=True,
    )
    thread.start()
    return jsonify({
        "status": "started",
        "mode": mode,
        "account": account,
        "message": f"Scrape [{mode}] iniciado en segundo plano{f' para {account}' if account else ''}.",
    })


@app.route("/api/trigger-status")
def trigger_status():
    """Estado del scrape bajo demanda."""
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    with _scrape_lock:
        return jsonify({
            "running":    _scrape_running,
            "mode":       _scrape_mode,
            "account":    _scrape_account,
            "last_error": _scrape_last_error,
        })


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    host = os.environ.get("VIEWER_HOST", "0.0.0.0")
    port = int(os.environ.get("VIEWER_PORT", "5001"))
    print(f"Viewer: http://localhost:{port}")
    if VIEWER_SECRET:
        print("  (Acceso protegido con VIEWER_SECRET)")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
