"""SQLite persistence layer.

Tables:
    settings         key/value config (seller id, API creds, base URL)
    api_products     rows fetched from Trendyol Seller API
    scraped_products rows scraped from the seller storefront
    jobs             one row per "Çalıştır" run
    job_logs         streaming log lines for the UI
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock


DB_PATH = os.environ.get("DATABASE_PATH", os.path.join("data", "app.db"))
_init_lock = Lock()
_initialized = False


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS api_products (
    product_id      TEXT PRIMARY KEY,
    product_main_id TEXT,
    barcode         TEXT,
    model           TEXT,
    color           TEXT,
    size            TEXT,
    category        TEXT,
    brand           TEXT,
    seller_code     TEXT,
    stock           INTEGER,
    vat_rate        REAL,
    list_price      REAL,
    sale_price      REAL,
    on_sale         INTEGER,
    archived        INTEGER,
    locked          INTEGER,
    lock_reason     TEXT,
    raw             TEXT,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_products_main ON api_products(product_main_id);

CREATE TABLE IF NOT EXISTS scraped_products (
    product_main_id TEXT PRIMARY KEY,
    product_url     TEXT,
    product_name    TEXT,
    model           TEXT,
    strike_price    REAL,
    sale_price      REAL,
    ty_plus_price   REAL,
    fav_count       INTEGER,
    review_count    INTEGER,
    rating          REAL,
    raw             TEXT,
    scraped_at      TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    status         TEXT,
    phase          TEXT,
    api_page       INTEGER,
    api_total_pages INTEGER,
    api_count      INTEGER,
    scrape_page    INTEGER,
    scrape_total_pages INTEGER,
    scrape_count   INTEGER,
    error          TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    claimed_by     TEXT,
    claimed_at     TEXT
);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id  TEXT PRIMARY KEY,
    last_seen  TEXT,
    info       TEXT
);

CREATE TABLE IF NOT EXISTS job_logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER,
    ts      TEXT,
    level   TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_logs_job ON job_logs(job_id, id);
"""


def _ensure_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def init_db() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        _ensure_dir(DB_PATH)
        with connection() as conn:
            conn.executescript(SCHEMA)
            # Idempotent migrations for tables created before new columns existed.
            for col in ("model TEXT",):
                try:
                    conn.execute(f"ALTER TABLE scraped_products ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass
            for col in ("claimed_by TEXT", "claimed_at TEXT"):
                try:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass
        _initialized = True


@contextmanager
def connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- settings ----------

SETTING_KEYS = [
    "seller_id",
    "api_key",
    "api_secret",
    "api_base_url",
    "page_size",
]


def get_settings() -> dict:
    with connection() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        data = {row["key"]: row["value"] for row in rows}
    out = {k: data.get(k, "") for k in SETTING_KEYS}
    return out


def update_settings(values: dict) -> None:
    with connection() as conn:
        for key in SETTING_KEYS:
            if key not in values:
                continue
            value = values.get(key)
            if value is None:
                continue
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )


# ---------- api_products ----------

def upsert_api_products(rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = (
        "INSERT INTO api_products ("
        "product_id, product_main_id, barcode, model, color, size, category, brand, "
        "seller_code, stock, vat_rate, list_price, sale_price, on_sale, archived, "
        "locked, lock_reason, raw, updated_at"
        ") VALUES (:product_id, :product_main_id, :barcode, :model, :color, :size, "
        ":category, :brand, :seller_code, :stock, :vat_rate, :list_price, :sale_price, "
        ":on_sale, :archived, :locked, :lock_reason, :raw, :updated_at) "
        "ON CONFLICT(product_id) DO UPDATE SET "
        "product_main_id=excluded.product_main_id, barcode=excluded.barcode, "
        "model=excluded.model, color=excluded.color, size=excluded.size, "
        "category=excluded.category, brand=excluded.brand, "
        "seller_code=excluded.seller_code, stock=excluded.stock, "
        "vat_rate=excluded.vat_rate, list_price=excluded.list_price, "
        "sale_price=excluded.sale_price, on_sale=excluded.on_sale, "
        "archived=excluded.archived, locked=excluded.locked, "
        "lock_reason=excluded.lock_reason, raw=excluded.raw, "
        "updated_at=excluded.updated_at"
    )
    ts = now_iso()
    payload = []
    for r in rows:
        payload.append({
            "product_id": str(r.get("product_id") or ""),
            "product_main_id": r.get("product_main_id"),
            "barcode": r.get("barcode"),
            "model": r.get("model"),
            "color": r.get("color"),
            "size": r.get("size"),
            "category": r.get("category"),
            "brand": r.get("brand"),
            "seller_code": r.get("seller_code"),
            "stock": r.get("stock"),
            "vat_rate": r.get("vat_rate"),
            "list_price": r.get("list_price"),
            "sale_price": r.get("sale_price"),
            "on_sale": int(bool(r.get("on_sale"))) if r.get("on_sale") is not None else None,
            "archived": int(bool(r.get("archived"))) if r.get("archived") is not None else None,
            "locked": int(bool(r.get("locked"))) if r.get("locked") is not None else None,
            "lock_reason": r.get("lock_reason"),
            "raw": r.get("raw"),
            "updated_at": ts,
        })
    with connection() as conn:
        conn.executemany(sql, payload)
    return len(payload)


def list_api_products(limit: int = 1000, offset: int = 0) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM api_products ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def count_api_products() -> int:
    with connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM api_products").fetchone()
        return int(row["c"]) if row else 0


# ---------- scraped_products ----------

def upsert_scraped_products(rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = (
        "INSERT INTO scraped_products ("
        "product_main_id, product_url, product_name, model, strike_price, sale_price, "
        "ty_plus_price, fav_count, review_count, rating, raw, scraped_at"
        ") VALUES (:product_main_id, :product_url, :product_name, :model, :strike_price, "
        ":sale_price, :ty_plus_price, :fav_count, :review_count, :rating, :raw, :scraped_at) "
        "ON CONFLICT(product_main_id) DO UPDATE SET "
        "product_url=excluded.product_url, product_name=excluded.product_name, "
        "model=COALESCE(excluded.model, scraped_products.model), "
        "strike_price=excluded.strike_price, sale_price=excluded.sale_price, "
        "ty_plus_price=excluded.ty_plus_price, fav_count=excluded.fav_count, "
        "review_count=excluded.review_count, rating=excluded.rating, "
        "raw=excluded.raw, scraped_at=excluded.scraped_at"
    )
    ts = now_iso()
    payload = []
    for r in rows:
        payload.append({
            "product_main_id": str(r.get("product_main_id") or ""),
            "product_url": r.get("product_url"),
            "product_name": r.get("product_name"),
            "model": r.get("model"),
            "strike_price": r.get("strike_price"),
            "sale_price": r.get("sale_price"),
            "ty_plus_price": r.get("ty_plus_price"),
            "fav_count": r.get("fav_count"),
            "review_count": r.get("review_count"),
            "rating": r.get("rating"),
            "raw": r.get("raw"),
            "scraped_at": ts,
        })
    with connection() as conn:
        conn.executemany(sql, payload)
    return len(payload)


def list_scraped_products(limit: int = 1000, offset: int = 0) -> list[dict]:
    """Return scraped rows enriched with API product info via product_main_id."""
    sql = """
        SELECT s.*,
               a.model AS api_model,
               a.color AS api_color,
               a.category AS api_category,
               a.brand AS api_brand
        FROM scraped_products s
        LEFT JOIN api_products a ON a.product_main_id = s.product_main_id
        ORDER BY s.scraped_at DESC
        LIMIT ? OFFSET ?
    """
    with connection() as conn:
        rows = conn.execute(sql, (limit, offset)).fetchall()
        return [dict(r) for r in rows]


def count_scraped_products() -> int:
    with connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM scraped_products").fetchone()
        return int(row["c"]) if row else 0


# ---------- jobs ----------

def create_job() -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (status, phase, started_at) VALUES (?, ?, ?)",
            ("running", "starting", now_iso()),
        )
        return int(cur.lastrowid)


def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields.keys())
    values = list(fields.values()) + [job_id]
    with connection() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", values)


def finish_job(job_id: int, status: str = "done", error: str | None = None) -> None:
    update_job(job_id, status=status, phase="finished", error=error, finished_at=now_iso())


def get_job(job_id: int) -> dict | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def latest_job() -> dict | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def append_log(job_id: int, message: str, level: str = "info") -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO job_logs (job_id, ts, level, message) VALUES (?, ?, ?, ?)",
            (job_id, now_iso(), level, message),
        )


def find_pending_scrape_job() -> dict | None:
    """Return the oldest job whose API phase finished and that is now waiting
    for a local worker to do the scrape phase."""
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'awaiting_local_scrape' "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def claim_scrape_job(job_id: int, worker_id: str) -> bool:
    """Atomic claim: only succeeds if the job is still awaiting."""
    with connection() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='scraping_local', phase='scrape', "
            "claimed_by=?, claimed_at=? "
            "WHERE id = ? AND status = 'awaiting_local_scrape'",
            (worker_id, now_iso(), job_id),
        )
        return cur.rowcount == 1


def heartbeat(worker_id: str, info: str | None = None) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO worker_heartbeats(worker_id, last_seen, info) "
            "VALUES(?, ?, ?) ON CONFLICT(worker_id) DO UPDATE SET "
            "last_seen=excluded.last_seen, info=excluded.info",
            (worker_id, now_iso(), info),
        )


def latest_heartbeat() -> dict | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM worker_heartbeats ORDER BY last_seen DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_logs(job_id: int, after_id: int = 0, limit: int = 500) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT id, ts, level, message FROM job_logs "
            "WHERE job_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (job_id, after_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
