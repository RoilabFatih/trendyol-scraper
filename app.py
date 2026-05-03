import os
from flask import Flask, render_template, request, jsonify

from scraper import TrendyolScraper, ScraperError

app = Flask(__name__)
scraper = TrendyolScraper()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape", methods=["POST"])
def scrape():
    payload = request.get_json(silent=True) or request.form
    url = (payload.get("url") or "").strip()

    if not url:
        return jsonify({"ok": False, "error": "Lütfen bir Trendyol ürün linki girin."}), 400

    try:
        data = scraper.fetch_product(url)
    except ScraperError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Beklenmeyen hata: {exc}"}), 500

    return jsonify({"ok": True, "data": data})


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
