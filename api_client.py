"""Thin wrapper over Trendyol Seller (integration) API for product listing."""

from __future__ import annotations

import base64
import json
import requests


DEFAULT_BASE = "https://apigw.trendyol.com/integration"


class TrendyolApiError(Exception):
    pass


class TrendyolApiClient:
    def __init__(
        self,
        seller_id: str,
        api_key: str,
        api_secret: str,
        base_url: str | None = None,
        timeout: int = 30,
    ):
        if not (seller_id and api_key and api_secret):
            raise TrendyolApiError("seller_id, api_key ve api_secret zorunlu.")
        self.seller_id = str(seller_id).strip()
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        token = base64.b64encode(f"{api_key}:{api_secret}".encode("utf-8")).decode("ascii")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Basic {token}",
            "User-Agent": f"{self.seller_id} - SelfIntegration",
            "Accept": "application/json",
        })

    def list_products(self, page: int = 0, size: int = 200) -> dict:
        url = f"{self.base_url}/product/sellers/{self.seller_id}/products"
        params = {"page": page, "size": size}
        try:
            r = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise TrendyolApiError(f"API'ye ulaşılamadı: {exc}") from exc

        if r.status_code == 401 or r.status_code == 403:
            raise TrendyolApiError(
                f"API kimlik doğrulama hatası ({r.status_code}). "
                "API key/secret veya satıcı ID'sini kontrol edin."
            )
        if r.status_code >= 400:
            raise TrendyolApiError(f"API HTTP {r.status_code}: {r.text[:200]}")

        try:
            return r.json()
        except ValueError as exc:
            raise TrendyolApiError(f"Geçersiz JSON yanıtı: {exc}") from exc


def normalize_api_product(raw: dict) -> dict:
    """Normalize Trendyol API product payload into our schema."""
    if not isinstance(raw, dict):
        return {}

    # Pull size/color from the attributes array (locale dependent).
    size_val = None
    color_val = None
    for attr in raw.get("attributes") or []:
        if not isinstance(attr, dict):
            continue
        name = (attr.get("attributeName") or "").strip().lower()
        value = attr.get("attributeValue")
        if name in ("beden", "numara", "size") and not size_val:
            size_val = value
        elif name in ("renk", "color") and not color_val:
            color_val = value

    list_price = _to_float(raw.get("listPrice"))
    sale_price = _to_float(raw.get("salePrice"))

    locked = bool(raw.get("locked"))
    lock_reason = raw.get("blacklistedReason") or raw.get("lockedReason")
    if locked and not lock_reason:
        # Many payloads use blacklistedReasonDetails or rejectReason
        lock_reason = raw.get("blacklistedReasonDetails") or raw.get("rejectReason")

    return {
        "product_id": raw.get("id") or raw.get("productCode") or raw.get("barcode"),
        "product_main_id": raw.get("productMainId"),
        "barcode": raw.get("barcode"),
        "model": raw.get("title") or raw.get("productMainId"),
        "color": color_val or raw.get("color"),
        "size": size_val,
        "category": raw.get("categoryName"),
        "brand": raw.get("brand"),
        "seller_code": raw.get("stockCode") or raw.get("productCode"),
        "stock": _to_int(raw.get("quantity") or raw.get("stockQuantity")),
        "vat_rate": _to_float(raw.get("vatRate")),
        "list_price": list_price,
        "sale_price": sale_price,
        "on_sale": raw.get("onSale"),
        "archived": raw.get("archived"),
        "locked": locked,
        "lock_reason": lock_reason,
        "raw": json.dumps(raw, ensure_ascii=False),
    }


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
