"""Local worker that watches the Railway panel for pending storefront
scrape jobs and runs them on this machine (so Trendyol's WAF sees a
Turkish IP). Long-running daemon — leave it open in a console window or
launch via `start_worker.bat`.

Flow per cycle (every POLL_INTERVAL seconds):
  1. POST /api/worker/heartbeat                 — let the panel know we're alive
  2. GET  /api/worker/scrape-pending            — any awaiting job?
  3. POST /api/worker/scrape-claim/<id>         — atomic grab
  4. Walk listing pages → enrich with productCode → push in batches
     while POSTing /api/worker/scrape-progress every page
  5. POST /api/worker/scrape-finish/<id>        — done or error
"""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

# Force UTF-8 stdout for Windows cp1254 consoles.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from storefront_scraper import SellerStorefrontScraper, StorefrontScraperError


CONFIG_PATH_DEFAULT = "local_config.json"
POLL_INTERVAL = 5             # seconds between checks
HEARTBEAT_EVERY = 4           # heartbeat every Nth poll cycle
INGEST_BATCH = 100
ENRICHMENT_WORKERS = 8


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        sys.exit(
            f"❌ Config dosyası bulunamadı: {path}\n"
            f"   `local_config.example.json` dosyasını `{path}` olarak kopyalayıp doldurun."
        )
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k in ("panel_url", "access_token"):
        if not cfg.get(k):
            sys.exit(f"❌ Config'de eksik: {k}")
    cfg["panel_url"] = cfg["panel_url"].rstrip("/")
    cfg.setdefault("scrape_max_pages", 200)
    return cfg


def make_session(panel_url: str, token: str) -> requests.Session:
    s = requests.Session()
    host = urlparse(panel_url).netloc
    s.cookies.set("ts_access", token, domain=host, secure=True)
    s.headers.update({"User-Agent": f"TrendyolPanelLocalWorker/1.0 ({platform.system()})"})
    return s


def worker_id() -> str:
    return f"{socket.gethostname()}#{os.getpid()}"


def log(msg: str, level: str = "info") -> None:
    prefix = {"info": "·", "warn": "!", "error": "✗", "ok": "✓"}.get(level, "·")
    print(f"[{time.strftime('%H:%M:%S')}] {prefix} {msg}", flush=True)


def push(panel: requests.Session, panel_url: str, path: str, payload=None,
         method: str = "POST"):
    url = f"{panel_url}{path}"
    r = panel.request(method, url, json=payload, timeout=60)
    if r.status_code == 401:
        raise RuntimeError("Panele auth başarısız. access_token doğru mu?")
    if r.status_code >= 400:
        raise RuntimeError(f"{path} HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except Exception:
        return {}


def heartbeat(panel, panel_url, wid):
    try:
        push(panel, panel_url, "/api/worker/heartbeat",
             {"worker_id": wid, "info": platform.platform()})
    except Exception as exc:
        log(f"heartbeat hatası: {exc}", level="warn")


def run_scrape(panel, cfg, job, wid):
    """Run the scrape phase for one job. Returns (status, error)."""
    panel_url = cfg["panel_url"]
    job_id = job["job_id"]
    seller_id = job["seller_id"]
    if not seller_id:
        msg = "Panel'de satıcı ID kayıtlı değil."
        log(msg, level="error")
        return "error", msg
    log(f"İş #{job_id} alındı — satıcı {seller_id} taranıyor")

    scraper = SellerStorefrontScraper()
    collected: list[dict] = []
    total_pages = 0

    def report_progress(page=None, total=None, count=None, message=None, level="info"):
        body = {}
        if page is not None: body["scrape_page"] = page
        if total is not None: body["scrape_total_pages"] = total
        if count is not None: body["scrape_count"] = count
        if message: body["message"] = message
        body["level"] = level
        try:
            push(panel, panel_url, f"/api/worker/scrape-progress/{job_id}", body)
        except Exception as exc:
            log(f"progress push hata: {exc}", level="warn")

    def on_page(page, total_pages_, page_count, cumulative):
        nonlocal total_pages
        total_pages = total_pages_ or total_pages
        log(f"  liste sayfa {page}/{total_pages or '?'} — {page_count} ürün (toplam {cumulative})")
        report_progress(page=page, total=total_pages or 0, count=cumulative,
                        message=f"Tarama sayfa {page}/{total_pages or '?'}: "
                                f"{page_count} ürün okundu (toplam {cumulative}).")

    try:
        for prod in scraper.iter_products(
            seller_id, on_page=on_page,
            max_pages=int(cfg.get("scrape_max_pages") or 200),
        ):
            collected.append(prod)
    except StorefrontScraperError as exc:
        return "error", str(exc)

    log(f"📋  {len(collected)} ürün listelendi.")
    if not collected:
        return "done", None

    # Parallel productCode enrichment.
    log(f"🔍  Detay sayfa enrichment başlıyor ({ENRICHMENT_WORKERS} paralel)…")
    workers = [SellerStorefrontScraper() for _ in range(ENRICHMENT_WORKERS)]
    counter = {"i": 0}

    def fetch_one(item):
        idx = counter["i"]; counter["i"] = idx + 1
        wsc = workers[idx % ENRICHMENT_WORKERS]
        item["model"] = wsc.fetch_product_code(item.get("product_url") or "")
        return item

    done = 0
    with ThreadPoolExecutor(max_workers=ENRICHMENT_WORKERS) as ex:
        for _ in as_completed([ex.submit(fetch_one, p) for p in collected]):
            done += 1
            if done % 25 == 0 or done == len(collected):
                report_progress(message=f"Detay sayfa {done}/{len(collected)}")
                log(f"  detay {done}/{len(collected)}")

    # Push to ingest in batches.
    pushed = 0
    for i in range(0, len(collected), INGEST_BATCH):
        chunk = collected[i : i + INGEST_BATCH]
        try:
            res = push(panel, panel_url, "/api/ingest/scraped-products",
                       {"rows": chunk})
            pushed += int(res.get("inserted", 0))
            report_progress(count=pushed)
        except Exception as exc:
            return "error", f"ingest hatası: {exc}"

    log(f"✅  {pushed} ürün ingest edildi.", level="ok")
    return "done", None


def main():
    cfg = load_config(CONFIG_PATH_DEFAULT)
    wid = worker_id()
    log(f"Yerel ajan başlıyor — id={wid}")
    log(f"Panel: {cfg['panel_url']}")
    panel = make_session(cfg["panel_url"], cfg["access_token"])

    # Quick auth test
    try:
        push(panel, cfg["panel_url"], "/api/settings", method="GET")
    except Exception as exc:
        sys.exit(f"❌ Panele bağlanılamadı: {exc}")
    log("Panele bağlanıldı. Bekleyen iş aranıyor…", level="ok")

    cycle = 0
    while True:
        cycle += 1
        try:
            if cycle % HEARTBEAT_EVERY == 1:
                heartbeat(panel, cfg["panel_url"], wid)

            res = push(panel, cfg["panel_url"], "/api/worker/scrape-pending",
                       method="GET")
            job = res.get("data") if isinstance(res, dict) else None
            if not job:
                time.sleep(POLL_INTERVAL)
                continue

            log(f"Bekleyen iş bulundu — #{job['job_id']}")
            try:
                push(panel, cfg["panel_url"],
                     f"/api/worker/scrape-claim/{job['job_id']}",
                     {"worker_id": wid})
            except Exception as exc:
                log(f"claim başarısız: {exc}", level="warn")
                time.sleep(POLL_INTERVAL)
                continue

            try:
                status, error = run_scrape(panel, cfg, job, wid)
            except Exception as exc:  # noqa: BLE001
                status = "error"
                error = f"Beklenmeyen hata: {exc}\n{traceback.format_exc(limit=4)}"
                log(error, level="error")

            try:
                push(panel, cfg["panel_url"],
                     f"/api/worker/scrape-finish/{job['job_id']}",
                     {"status": status, "error": error})
            except Exception as exc:
                log(f"finish push hata: {exc}", level="warn")

            log("Tekrar bekleme moduna geçiliyor…")

        except KeyboardInterrupt:
            log("Çıkış istendi, durduruluyor…")
            return
        except Exception as exc:  # noqa: BLE001
            log(f"poll döngüsü hata: {exc}", level="error")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
