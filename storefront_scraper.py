"""Scrape a Trendyol seller's storefront listing pages.

The seller listing URL is `https://www.trendyol.com/sr?mid={sellerId}&pi={page}`.
Each page embeds the product list as JSON inside the HTML (under the search
results state). We walk pages until no more products are returned.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Iterable
from urllib.parse import urlencode

import requests

# Trendyol's Cloudflare WAF rejects the default Python TLS fingerprint with
# HTTP 403 even when sent from a Turkish IP. curl_cffi impersonates a real
# Chrome TLS handshake and gets through. We use it transparently when it's
# installed; otherwise we fall back to plain requests (and you'll need a
# proxy or to install curl_cffi to bypass Cloudflare).
try:
    from curl_cffi import requests as cffi_requests  # type: ignore
    _HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover
    cffi_requests = None  # type: ignore
    _HAS_CURL_CFFI = False


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


P_PATTERN = re.compile(r"-p-(\d+)")


class StorefrontScraperError(Exception):
    pass


class SellerStorefrontScraper:
    BASE = "https://www.trendyol.com/sr"

    def __init__(self, timeout: int = 25, proxy: str | None = None):
        self.timeout = timeout
        # Prefer curl_cffi for Cloudflare TLS fingerprint bypass when available.
        if _HAS_CURL_CFFI:
            self.session = cffi_requests.Session(impersonate="chrome124")
        else:
            self.session = requests.Session()
        self.session.headers.update(HEADERS)
        # Optional outbound proxy: route through a Turkish residential / mobile
        # proxy if Trendyol's WAF is blocking the server's datacenter IP.
        proxy = proxy or os.environ.get("TRENDYOL_PROXY") or os.environ.get("HTTPS_PROXY")
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def fetch_page(self, seller_id: str, page: int = 1) -> dict:
        params = {"mid": str(seller_id).strip(), "pi": page}
        url = f"{self.BASE}?{urlencode(params)}"
        try:
            r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        except Exception as exc:  # noqa: BLE001 — curl_cffi raises its own exceptions
            raise StorefrontScraperError(f"İstek başarısız: {exc}") from exc

        if r.status_code == 403:
            raise StorefrontScraperError(
                "HTTP 403 — Trendyol bu sunucunun IP'sinden gelen istekleri engelledi "
                "(genelde TR-dışı veri merkezi IP'leri Cloudflare WAF tarafından bloklanır). "
                "Çözüm: bir TR residential/mobile proxy ayarlayın "
                "(env var: TRENDYOL_PROXY=http://user:pass@host:port) ya da satıcı "
                "Seller API'si üzerinden çekim yapın."
            )
        if r.status_code != 200:
            raise StorefrontScraperError(f"HTTP {r.status_code}")

        return _parse_listing_html(r.text, page)

    def iter_products(
        self,
        seller_id: str,
        on_page: callable | None = None,
        max_pages: int = 200,
        sleep: float = 0.4,
    ) -> Iterable[dict]:
        page = 1
        seen_ids: set[str] = set()
        total_pages: int | None = None
        while page <= max_pages:
            data = self.fetch_page(seller_id, page=page)
            products = data.get("products") or []
            if total_pages is None:
                total_pages = data.get("total_pages") or 0
            if on_page:
                on_page(page=page, total_pages=total_pages or 0,
                        page_count=len(products),
                        cumulative=len(seen_ids) + len(products))
            new_count = 0
            for prod in products:
                pid = prod.get("product_main_id")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                new_count += 1
                yield prod
            if new_count == 0:
                # Either truly empty page or fully duplicated → stop.
                break
            if total_pages and page >= total_pages:
                break
            page += 1
            if sleep:
                time.sleep(sleep)


# ---------------- HTML parsing ----------------

def _parse_listing_html(html: str, page_no: int) -> dict:
    """Extract product list from a Trendyol search/seller listing page."""
    products: list[dict] = []
    total_pages = 0

    # Look for "products":[ ... ] inline JSON block.
    products_block = _extract_json_value(html, "products")
    if isinstance(products_block, list):
        for item in products_block:
            normalized = _normalize_listing_item(item)
            if normalized:
                products.append(normalized)

    # totalCount / totalPage / pages
    pi_block = _extract_json_value(html, "pageInfo")
    if isinstance(pi_block, dict):
        total_pages = int(pi_block.get("totalPages") or pi_block.get("totalPage") or 0)
    if not total_pages:
        # fallback "totalPages":N
        m = re.search(r'"totalPages?"\s*:\s*(\d+)', html)
        if m:
            total_pages = int(m.group(1))

    return {"page": page_no, "total_pages": total_pages, "products": products}


def _normalize_listing_item(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    url_raw = item.get("url") or ""
    if isinstance(url_raw, str) and url_raw.startswith("/"):
        url = f"https://www.trendyol.com{url_raw}"
    else:
        url = url_raw or ""

    p_id = None
    m = P_PATTERN.search(url)
    if m:
        p_id = m.group(1)
    if not p_id:
        # productGroupId or contentId field
        p_id = item.get("productGroupId") or item.get("id") or item.get("contentId")

    price_block = item.get("price") or {}
    if isinstance(price_block, dict):
        sale = (
            (price_block.get("discountedPrice") or {}).get("value")
            if isinstance(price_block.get("discountedPrice"), dict)
            else price_block.get("discountedPrice")
        )
        strike = (
            (price_block.get("originalPrice") or {}).get("value")
            if isinstance(price_block.get("originalPrice"), dict)
            else price_block.get("originalPrice")
        )
        ty_plus = (
            (price_block.get("tyPlusPrice") or {}).get("value")
            if isinstance(price_block.get("tyPlusPrice"), dict)
            else price_block.get("tyPlusPrice")
        )
    else:
        sale = strike = ty_plus = None

    rating_block = item.get("ratingScore") or {}
    if isinstance(rating_block, dict):
        rating = rating_block.get("averageRating") or rating_block.get("value")
        review = rating_block.get("totalRatingCount") or rating_block.get("totalCount")
    else:
        rating = item.get("rating")
        review = item.get("commentCount") or item.get("reviewCount")

    name = (
        item.get("name")
        or " ".join(filter(None, [item.get("brand", ""), item.get("productName", "")]))
        or item.get("productName")
        or item.get("title")
    )

    fav = (
        item.get("favoriteCount")
        or item.get("favouriteCount")
        or item.get("favoriteOperationCount")
    )

    return {
        "product_main_id": str(p_id) if p_id else None,
        "product_url": url,
        "product_name": name,
        "strike_price": _to_float(strike),
        "sale_price": _to_float(sale),
        "ty_plus_price": _to_float(ty_plus),
        "fav_count": _to_int(fav),
        "review_count": _to_int(review),
        "rating": _to_float(rating),
        "raw": json.dumps(item, ensure_ascii=False)[:4000],
    }


# Reuse the same balanced-brace JSON extractor that scraper.py uses.

def _extract_json_value(text: str, key: str):
    needle = f'"{key}"'
    pos = 0
    while True:
        idx = text.find(needle, pos)
        if idx < 0:
            return None
        i = idx + len(needle)
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text) or text[i] != ":":
            pos = idx + len(needle)
            continue
        i += 1
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text) or text[i] not in "{[":
            pos = idx + len(needle)
            continue
        snippet = _read_balanced(text, i)
        if not snippet:
            return None
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            pos = i + 1
            continue


def _read_balanced(text: str, start: int) -> str | None:
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    i = start
    while i < len(text):
        c = text[i]
        if escape:
            escape = False
        elif c == "\\":
            escape = True
        elif in_string:
            if c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def _to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
