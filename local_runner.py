"""Run the API + storefront scrape on this laptop and stream results to
the Railway panel via the ingest endpoints. Use this when Railway's
datacenter IP is blocked by Trendyol's WAF.

Usage:
    1. Copy `local_config.example.json` to `local_config.json` and fill in
       your panel URL, access token, seller ID, API key/secret.
    2. Run `python local_runner.py` (or double-click `run_local.bat`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Force UTF-8 stdout so Turkish chars + emoji don't crash on Windows cp1254.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from typing import Iterable
from urllib.parse import urlparse

import requests

from api_client import TrendyolApiClient, TrendyolApiError, normalize_api_product
from storefront_scraper import SellerStorefrontScraper, StorefrontScraperError


CONFIG_PATH_DEFAULT = "local_config.json"
INGEST_BATCH = 100


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        sys.exit(
            f"❌ Config dosyası bulunamadı: {path}\n"
            f"   `local_config.example.json` dosyasını `{path}` olarak kopyalayıp doldurun."
        )
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    required = ("panel_url", "access_token", "seller_id")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit(f"❌ Config'de eksik alanlar: {', '.join(missing)}")

    cfg["panel_url"] = cfg["panel_url"].rstrip("/")
    cfg.setdefault("page_size", 200)
    cfg.setdefault("scrape_max_pages", 200)
    cfg.setdefault("api_base_url", "")
    cfg.setdefault("api_key", "")
    cfg.setdefault("api_secret", "")
    return cfg


def make_panel_session(panel_url: str, token: str) -> requests.Session:
    """Authenticate to the panel by setting the access cookie."""
    s = requests.Session()
    host = urlparse(panel_url).netloc
    s.cookies.set("ts_access", token, domain=host, secure=True)
    s.headers.update({"User-Agent": "TrendyolPanelLocalRunner/1.0"})
    return s


def push_rows(session: requests.Session, panel_url: str, kind: str, rows: list[dict]) -> dict:
    """kind = 'api-products' or 'scraped-products'"""
    if not rows:
        return {"inserted": 0}
    url = f"{panel_url}/api/ingest/{kind}"
    r = session.post(url, json={"rows": rows}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"ingest {kind} HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def line(msg: str) -> None:
    print(msg, flush=True)


def progress(msg: str) -> None:
    # Same-line update
    sys.stdout.write("\r\033[K" + msg)
    sys.stdout.flush()


def run_api_phase(cfg: dict, panel: requests.Session) -> int:
    if not (cfg["api_key"] and cfg["api_secret"]):
        line("⚠️  API key/secret yok — API aşaması atlanıyor.")
        return 0

    line(f"\n🔌  API aşaması başlıyor (sayfa boyutu={cfg['page_size']})")
    client = TrendyolApiClient(
        seller_id=cfg["seller_id"],
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        base_url=cfg["api_base_url"] or None,
    )
    page = 0
    total_pages = None
    pushed = 0
    while True:
        try:
            payload = client.list_products(page=page, size=int(cfg["page_size"]))
        except TrendyolApiError as exc:
            line(f"\n❌  API hatası: {exc}")
            raise
        content = payload.get("content") or []
        if total_pages is None:
            total_pages = int(payload.get("totalPages") or 0)
        normalized = [normalize_api_product(p) for p in content if isinstance(p, dict)]
        normalized = [n for n in normalized if n and n.get("product_id")]
        # Push in batches
        for i in range(0, len(normalized), INGEST_BATCH):
            chunk = normalized[i : i + INGEST_BATCH]
            res = push_rows(panel, cfg["panel_url"], "api-products", chunk)
            pushed += int(res.get("inserted", 0))
        progress(
            f"   API sayfa {page + 1}/{total_pages or '?'}  "
            f"sayfa içi {len(content)} ürün  toplam itildi {pushed}"
        )
        if not content or (total_pages and page + 1 >= total_pages):
            break
        page += 1
    line("")
    line(f"✅  API tamam — {pushed} ürün ingest edildi.")
    return pushed


def run_scrape_phase(cfg: dict, panel: requests.Session) -> int:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    line(f"\n🕷️  Tarama aşaması başlıyor (mid={cfg['seller_id']})")
    scraper = SellerStorefrontScraper()
    enrichment_workers = int(cfg.get("enrichment_workers", 8))

    # Step 1: walk listing pages and collect every product
    collected: list[dict] = []

    def on_page(page, total_pages, page_count, cumulative):
        progress(
            f"   Liste sayfa {page}/{total_pages or '?'}  "
            f"sayfa içi {page_count}  birikmiş {cumulative}"
        )

    try:
        for prod in scraper.iter_products(
            cfg["seller_id"],
            on_page=on_page,
            max_pages=int(cfg["scrape_max_pages"]),
        ):
            collected.append(prod)
    except StorefrontScraperError as exc:
        line(f"\n❌  Tarama hatası: {exc}")
        raise
    line("")
    line(f"📋  {len(collected)} ürün listelendi.")

    # Step 2: enrich each product with its productCode (model) by
    #          fetching the detail page in parallel.
    if collected and not cfg.get("skip_model_enrichment"):
        line(f"🔍  Model (productCode) bilgisi her ürün için detay sayfasından çekiliyor "
             f"({enrichment_workers} paralel)…")

        # Each worker thread should use its own scraper / session for safety.
        worker_scrapers = [SellerStorefrontScraper() for _ in range(enrichment_workers)]
        worker_index = {0: 0}  # naive round-robin via a shared counter

        def fetch_one(item: dict) -> dict:
            wsc = worker_scrapers[worker_index[0] % enrichment_workers]
            worker_index[0] += 1
            code = wsc.fetch_product_code(item.get("product_url") or "")
            item["model"] = code
            return item

        done = 0
        with ThreadPoolExecutor(max_workers=enrichment_workers) as ex:
            futures = [ex.submit(fetch_one, p) for p in collected]
            for _ in as_completed(futures):
                done += 1
                if done % 10 == 0 or done == len(collected):
                    progress(f"   Detay sayfa {done}/{len(collected)}")
        line("")

    # Step 3: push everything in batches.
    pushed = 0
    for i in range(0, len(collected), INGEST_BATCH):
        chunk = collected[i : i + INGEST_BATCH]
        res = push_rows(panel, cfg["panel_url"], "scraped-products", chunk)
        pushed += int(res.get("inserted", 0))
        progress(f"   Push {min(i + INGEST_BATCH, len(collected))}/{len(collected)}")
    line("")
    line(f"✅  Tarama tamam — {pushed} ürün ingest edildi.")
    return pushed


def main():
    parser = argparse.ArgumentParser(description="Trendyol local runner")
    parser.add_argument("-c", "--config", default=CONFIG_PATH_DEFAULT)
    parser.add_argument("--api-only", action="store_true", help="Sadece API aşaması")
    parser.add_argument("--scrape-only", action="store_true", help="Sadece tarama aşaması")
    args = parser.parse_args()

    cfg = load_config(args.config)
    line(f"🌐 Panel: {cfg['panel_url']}")
    line(f"🏷️  Satıcı: {cfg['seller_id']}")

    panel = make_panel_session(cfg["panel_url"], cfg["access_token"])

    # Quick auth check
    r = panel.get(f"{cfg['panel_url']}/api/settings", timeout=15)
    if r.status_code != 200:
        sys.exit(
            f"❌ Panel kimlik doğrulaması başarısız (HTTP {r.status_code}).\n"
            f"   access_token doğru mu? config: {args.config}"
        )
    line("✅ Panele bağlanıldı.")

    started = time.time()
    api_count = 0
    scrape_count = 0
    try:
        if not args.scrape_only:
            api_count = run_api_phase(cfg, panel)
        if not args.api_only:
            scrape_count = run_scrape_phase(cfg, panel)
    except Exception as exc:  # noqa: BLE001
        line(f"\n❌ İş hata ile sonlandı: {exc}")
        sys.exit(1)

    elapsed = time.time() - started
    line(f"\n🎉 Bitti — {api_count} API + {scrape_count} tarama ürünü, {elapsed:.1f}s")


if __name__ == "__main__":
    main()
