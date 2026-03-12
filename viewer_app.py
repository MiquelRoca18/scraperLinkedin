"""
API mínima para listar y servir los CSV y runs del scraper, y para lanzar un scrape bajo demanda.
Sirve también un frontend para ver los datos y ejecutar un scrape desde el navegador.

Uso (local o en servidor):
  pip install -r requirements.txt
  python viewer_app.py

Abre http://localhost:5001 (o http://IP:5001 en la nube).
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


def _check_auth():
    if not VIEWER_SECRET:
        return True
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip() or request.args.get("token", "").strip()
    return token == VIEWER_SECRET


def _safe_filename(name: str) -> bool:
    """Comprueba que el nombre no tenga path traversal."""
    return name and "/" not in name and "\\" not in name and ".." not in name


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


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
                rows = max(0, sum(1 for _ in fp) - 1)  # menos cabecera
        except Exception:
            pass
        files.append({
            "name": f.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "rows": rows,
        })
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
            return send_file(buf, as_attachment=True, download_name=xlsx_name, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
        df = df.fillna("")   # NaN -> "" para que sea JSON válido
        return jsonify(df.to_dict(orient="records"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/runs")
def list_runs():
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    if not Path(DB_PATH).exists():
        return jsonify([])
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, username, started_at, finished_at, contacts_scraped, contacts_new, contacts_updated FROM runs ORDER BY id DESC LIMIT 100"
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _run_scrape_background():
    global _scrape_running, _scrape_last_error
    try:
        from main import run_scrape
        run_scrape(interactive=False)
        with _scrape_lock:
            _scrape_last_error = None
    except Exception as e:
        with _scrape_lock:
            _scrape_last_error = str(e)
    finally:
        with _scrape_lock:
            _scrape_running = False


@app.route("/api/trigger-scrape", methods=["POST"])
def trigger_scrape():
    """Lanza un scrape ahora (en segundo plano). No espera al siguiente cron."""
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    global _scrape_running
    with _scrape_lock:
        if _scrape_running:
            return jsonify({"error": "Ya hay un scrape en ejecución", "running": True}), 409
        _scrape_running = True
    thread = threading.Thread(target=_run_scrape_background, daemon=True)
    thread.start()
    return jsonify({"status": "started", "message": "Scrape iniciado en segundo plano"})


@app.route("/api/trigger-status")
def trigger_status():
    """Estado del scrape bajo demanda: running y último error (si hubo)."""
    if not _check_auth():
        return jsonify({"error": "No autorizado"}), 401
    with _scrape_lock:
        return jsonify({
            "running": _scrape_running,
            "last_error": _scrape_last_error,
        })


def main():
    host = os.environ.get("VIEWER_HOST", "0.0.0.0")
    port = int(os.environ.get("VIEWER_PORT", "5001"))
    print(f"Viewer: http://localhost:{port}")
    if VIEWER_SECRET:
        print("  (Acceso protegido con VIEWER_SECRET)")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
