import os
import secrets
from urllib.parse import urlencode

from flask import (
    Flask, jsonify, redirect, render_template, request, make_response,
)

import db
import job_runner
from scraper import ScraperError, TrendyolScraper

app = Flask(__name__)
db.init_db()

product_scraper = TrendyolScraper()


# ---------------- access gate ----------------

ACCESS_TOKEN = (os.environ.get("ACCESS_TOKEN") or "").strip()
COOKIE_NAME = "ts_access"
COOKIE_MAX_AGE = 60 * 60 * 24 * 180  # 180 days
PUBLIC_PATHS = {"/healthz"}


@app.before_request
def gate():
    if not ACCESS_TOKEN:
        return  # protection disabled — fail open if env var missing
    if request.path in PUBLIC_PATHS:
        return

    qtoken = request.args.get("token", "")
    if qtoken and secrets.compare_digest(qtoken, ACCESS_TOKEN):
        # Strip the token from the URL so it isn't bookmarked / leaked.
        clean = request.args.to_dict(flat=True)
        clean.pop("token", None)
        target = request.path + (("?" + urlencode(clean)) if clean else "")
        resp = make_response(redirect(target))
        resp.set_cookie(
            COOKIE_NAME, ACCESS_TOKEN,
            max_age=COOKIE_MAX_AGE,
            secure=True, httponly=True, samesite="Lax",
        )
        return resp

    cookie = request.cookies.get(COOKIE_NAME, "")
    if cookie and secrets.compare_digest(cookie, ACCESS_TOKEN):
        return  # allow

    return _denied_response()


def _denied_response():
    html = (
        "<!doctype html><html lang=tr><meta charset=utf-8>"
        "<title>Erişim engellendi</title>"
        "<style>body{margin:0;min-height:100vh;display:flex;align-items:center;"
        "justify-content:center;font-family:system-ui,-apple-system,sans-serif;"
        "background:#0f172a;color:#f1f5f9}"
        ".box{max-width:420px;padding:2rem;text-align:center;"
        "border:1px solid #334155;border-radius:.6rem;background:#1e293b}"
        "h1{margin:0 0 .5rem;font-size:1.1rem;color:#f97316}"
        "p{color:#94a3b8;font-size:.9rem;line-height:1.5;margin:0}"
        "</style><div class=box>"
        "<h1>🔒 Erişim engellendi</h1>"
        "<p>Bu paneli görüntülemek için size verilen tam linki kullanmanız gerekir.</p>"
        "</div></html>"
    )
    return html, 401, {"Content-Type": "text/html; charset=utf-8"}


# ---------------- pages ----------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


@app.route("/api/_debug/probe")
def debug_probe():
    """Diagnose 403s: hit Trendyol from this server and report what it sees."""
    import requests
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "tr-TR,tr;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    targets = {
        "homepage": "https://www.trendyol.com/",
        "search": "https://www.trendyol.com/sr?q=tisort",
        "seller": "https://www.trendyol.com/sr?mid=901409&pi=1",
    }
    out = {}
    for name, url in targets.items():
        try:
            r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            out[name] = {
                "status": r.status_code,
                "bytes": len(r.content),
                "server": r.headers.get("server"),
                "via": r.headers.get("via"),
                "x_country": r.headers.get("x-country") or r.headers.get("cf-ipcountry"),
            }
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": str(exc)}
    # Also report this server's outbound IP via a public echo service
    try:
        ip_r = requests.get("https://api.ipify.org?format=json", timeout=10)
        out["our_ip"] = ip_r.json().get("ip")
    except Exception as exc:  # noqa: BLE001
        out["our_ip"] = f"err: {exc}"
    return out


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
