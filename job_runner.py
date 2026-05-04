"""Run a "fetch API + scrape storefront" job in a background thread.

Only one job is allowed to run at a time. The status, progress, and log lines
are persisted to SQLite so the UI can poll them.
"""

from __future__ import annotations

import threading
import traceback
from typing import Optional

import db
from api_client import TrendyolApiClient, TrendyolApiError, normalize_api_product
from storefront_scraper import SellerStorefrontScraper, StorefrontScraperError


_lock = threading.Lock()
_active_job_id: Optional[int] = None
_thread: Optional[threading.Thread] = None


class JobBusyError(Exception):
    pass


def is_running() -> bool:
    return bool(_thread and _thread.is_alive())


def active_job_id() -> Optional[int]:
    return _active_job_id


def start_job() -> int:
    """Kick off a new job. Raises JobBusyError if one is already running."""
    global _active_job_id, _thread
    with _lock:
        if is_running():
            raise JobBusyError("Önceki iş hâlâ çalışıyor.")
        settings = db.get_settings()
        job_id = db.create_job()
        _active_job_id = job_id
        _thread = threading.Thread(
            target=_run, args=(job_id, settings), daemon=True, name=f"job-{job_id}"
        )
        _thread.start()
        return job_id


def _log(job_id: int, msg: str, level: str = "info") -> None:
    db.append_log(job_id, msg, level=level)


def _run(job_id: int, settings: dict) -> None:
    try:
        seller_id = (settings.get("seller_id") or "").strip()
        api_key = (settings.get("api_key") or "").strip()
        api_secret = (settings.get("api_secret") or "").strip()
        api_base = (settings.get("api_base_url") or "").strip() or None
        try:
            page_size = int(settings.get("page_size") or 200)
        except (TypeError, ValueError):
            page_size = 200
        page_size = max(10, min(page_size, 1000))

        if not seller_id:
            raise RuntimeError("Satıcı ID boş — Parametreler sekmesinden ekleyin.")

        # ---------- Phase 1: API ----------
        if api_key and api_secret:
            db.update_job(job_id, phase="api")
            _log(job_id, f"API çağrıları başlıyor (sayfa boyutu={page_size}).")
            client = TrendyolApiClient(
                seller_id=seller_id,
                api_key=api_key,
                api_secret=api_secret,
                base_url=api_base,
            )
            page = 0
            total_pages = None
            api_total = 0
            while True:
                _log(job_id, f"API isteği #{page + 1} gönderiliyor.")
                try:
                    payload = client.list_products(page=page, size=page_size)
                except TrendyolApiError as exc:
                    _log(job_id, f"API hatası: {exc}", level="error")
                    raise
                content = payload.get("content") or []
                if total_pages is None:
                    total_pages = int(payload.get("totalPages") or 0)
                normalized = [normalize_api_product(p) for p in content if isinstance(p, dict)]
                normalized = [n for n in normalized if n and n.get("product_id")]
                inserted = db.upsert_api_products(normalized)
                api_total += inserted
                db.update_job(
                    job_id,
                    api_page=page + 1,
                    api_total_pages=total_pages or 0,
                    api_count=api_total,
                )
                _log(
                    job_id,
                    f"API sayfa {page + 1}/{total_pages or '?'}: {len(content)} ürün geldi, "
                    f"{inserted} kayıt güncellendi (toplam {api_total}).",
                )
                if not content:
                    break
                if total_pages and page + 1 >= total_pages:
                    break
                page += 1
            _log(job_id, f"API tamam. Toplam {api_total} ürün kaydı.")
        else:
            _log(
                job_id,
                "API key/secret girilmemiş — API aşaması atlanıyor.",
                level="warn",
            )

        # ---------- Phase 2: hand off scrape to a local worker ----------
        # Trendyol's Cloudflare WAF blocks non-TR datacenter IPs from the
        # storefront, so we don't scrape from Railway. Instead we mark the
        # job as awaiting and let the user's local_worker.py pick it up.
        db.update_job(job_id, phase="awaiting_local_scrape", status="awaiting_local_scrape")
        _log(
            job_id,
            "API aşaması bitti. Tarama aşaması için yerel ajan bekleniyor "
            "(makinende `start_worker.bat` çalışıyor olmalı).",
        )

    except Exception as exc:
        tb = traceback.format_exc(limit=5)
        db.finish_job(job_id, status="error", error=str(exc))
        _log(job_id, f"İş hata ile sonlandı: {exc}\n{tb}", level="error")
    finally:
        global _active_job_id
        _active_job_id = None
