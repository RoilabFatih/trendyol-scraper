import os

from flask import Flask, jsonify, render_template, request

import db
import job_runner
from scraper import ScraperError, TrendyolScraper

app = Flask(__name__)
db.init_db()

product_scraper = TrendyolScraper()


# ---------------- pages ----------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


@app.route("/api/_debug/storage")
def debug_storage():
    """Diagnostic endpoint: report DB path / volume mount visibility."""
    info = {
        "env_DATABASE_PATH": os.environ.get("DATABASE_PATH"),
        "module_DB_PATH": db.DB_PATH,
        "cwd": os.getcwd(),
    }
    for path in ("/data", os.path.dirname(os.path.abspath(db.DB_PATH))):
        try:
            stat = os.statvfs(path) if hasattr(os, "statvfs") else None
            info[path] = {
                "exists": os.path.exists(path),
                "is_dir": os.path.isdir(path),
                "is_mount": os.path.ismount(path),
                "writable": os.access(path, os.W_OK) if os.path.exists(path) else False,
                "contents": (os.listdir(path) if os.path.isdir(path) else None),
                "free_mb": (stat.f_bavail * stat.f_frsize / (1024 * 1024)) if stat else None,
            }
        except Exception as exc:  # noqa: BLE001
            info[path] = {"error": str(exc)}
    info["db_file_exists"] = os.path.exists(db.DB_PATH)
    if info["db_file_exists"]:
        try:
            info["db_file_size"] = os.path.getsize(db.DB_PATH)
        except OSError:
            pass
    return info


# ---------------- single product (legacy) ----------------

@app.route("/api/scrape", methods=["POST"])
def scrape_single():
    payload = request.get_json(silent=True) or request.form
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Lütfen bir Trendyol ürün linki girin."}), 400
    try:
        data = product_scraper.fetch_product(url)
    except ScraperError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Beklenmeyen hata: {exc}"}), 500
    return jsonify({"ok": True, "data": data})


# ---------------- settings ----------------

@app.route("/api/settings", methods=["GET"])
def get_settings():
    settings = db.get_settings()
    safe = dict(settings)
    if safe.get("api_secret"):
        safe["api_secret_present"] = True
        safe["api_secret"] = ""
    else:
        safe["api_secret_present"] = False
    return jsonify({"ok": True, "data": safe})


@app.route("/api/settings", methods=["POST"])
def save_settings():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Geçersiz istek gövdesi."}), 400

    cleaned = {}
    for key in db.SETTING_KEYS:
        if key in payload:
            value = payload.get(key)
            if value is None:
                continue
            value = str(value).strip()
            # Don't overwrite secret with empty string (UI sends empty if untouched).
            if key == "api_secret" and not value:
                continue
            cleaned[key] = value
    db.update_settings(cleaned)
    return jsonify({"ok": True, "data": db.get_settings()})


# ---------------- jobs ----------------

@app.route("/api/jobs/start", methods=["POST"])
def start_job():
    try:
        job_id = job_runner.start_job()
    except job_runner.JobBusyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/jobs/status")
def job_status():
    job_id = request.args.get("id", type=int)
    if not job_id:
        latest = db.latest_job()
        if not latest:
            return jsonify({"ok": True, "data": None, "running": False})
        job_id = int(latest["id"])
    job = db.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "İş bulunamadı."}), 404
    after_id = request.args.get("after_log_id", default=0, type=int)
    logs = db.get_logs(job_id, after_id=after_id, limit=200)
    return jsonify({
        "ok": True,
        "data": job,
        "logs": logs,
        "running": job_runner.is_running() and job_runner.active_job_id() == job_id,
    })


# ---------------- data ----------------

@app.route("/api/data/api-products")
def api_products():
    limit = min(request.args.get("limit", default=500, type=int), 5000)
    offset = max(request.args.get("offset", default=0, type=int), 0)
    rows = db.list_api_products(limit=limit, offset=offset)
    total = db.count_api_products()
    return jsonify({"ok": True, "data": rows, "total": total})


@app.route("/api/data/scraped-products")
def scraped_products():
    limit = min(request.args.get("limit", default=500, type=int), 5000)
    offset = max(request.args.get("offset", default=0, type=int), 0)
    rows = db.list_scraped_products(limit=limit, offset=offset)
    total = db.count_scraped_products()
    return jsonify({"ok": True, "data": rows, "total": total})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
