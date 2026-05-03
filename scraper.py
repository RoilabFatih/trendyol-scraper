import json
import re
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


class ScraperError(Exception):
    pass


class TrendyolScraper:
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)

    def fetch_product(self, url: str) -> dict:
        self._validate_url(url)
        clean_url = self._clean_url(url)

        try:
            response = self.session.get(clean_url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            raise ScraperError(f"Sayfaya ulaşılamadı: {exc}") from exc

        if response.status_code != 200:
            raise ScraperError(f"Trendyol HTTP {response.status_code} döndürdü.")

        html = response.text
        soup = BeautifulSoup(html, "html.parser")

        ld = self._parse_jsonld(soup)
        meta_data = self._parse_meta(soup)

        primary = self._extract_primary_seller(html)
        others = self._extract_other_sellers(html)

        offer = (ld or {}).get("offers") or {}
        base_price = _to_float(offer.get("price")) or _to_float(offer.get("lowPrice"))
        if primary and base_price is not None:
            for variant in primary.get("variants", []):
                if variant.get("price") is None and variant.get("inStock"):
                    variant["price"] = base_price

        sizes = self._collect_size_index(primary, others)

        title = (
            (ld.get("name") if ld else None)
            or meta_data.get("title")
        )

        if not title and not primary and not others:
            raise ScraperError(
                "Ürün verisi okunamadı. Sayfa yapısı değişmiş olabilir veya link bir ürün sayfası değil."
            )

        brand = ((ld or {}).get("brand") or {}).get("name") if ld else None
        rating_obj = (ld or {}).get("aggregateRating") or {} if ld else {}

        images = []
        if ld:
            img_block = ld.get("image") or {}
            if isinstance(img_block, dict):
                images = img_block.get("contentUrl") or []
            elif isinstance(img_block, list):
                images = img_block
            elif isinstance(img_block, str):
                images = [img_block]
        if not images and meta_data.get("image"):
            images = [meta_data["image"]]

        currency = offer.get("priceCurrency") or "TRY"

        return {
            "id": (ld or {}).get("productGroupID") or (ld or {}).get("sku"),
            "title": title,
            "brand": brand,
            "description": (ld or {}).get("description") or meta_data.get("description"),
            "rating": _to_float(rating_obj.get("ratingValue")),
            "ratingCount": _to_int(rating_obj.get("reviewCount") or rating_obj.get("ratingCount")),
            "currency": currency,
            "url": clean_url,
            "images": images[:8],
            "primarySeller": primary,
            "otherSellers": others,
            "sizes": sizes,
        }

    def _validate_url(self, url: str) -> None:
        try:
            parsed = urlparse(url)
        except Exception as exc:
            raise ScraperError("Geçersiz URL.") from exc

        if parsed.scheme not in ("http", "https"):
            raise ScraperError("URL http veya https ile başlamalı.")

        host = (parsed.netloc or "").lower()
        if "trendyol.com" not in host:
            raise ScraperError("Sadece trendyol.com linkleri destekleniyor.")

    def _clean_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse(parsed._replace(query="", fragment=""))

    def _parse_jsonld(self, soup: BeautifulSoup) -> dict | None:
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            text = tag.string or tag.get_text() or ""
            text = text.strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                if isinstance(item, dict) and item.get("@type") in ("ProductGroup", "Product"):
                    return item
        return None

    def _parse_meta(self, soup: BeautifulSoup) -> dict:
        def meta(prop: str, attr: str = "property") -> str | None:
            tag = soup.find("meta", attrs={attr: prop})
            return tag.get("content") if tag and tag.get("content") else None

        return {
            "title": meta("og:title"),
            "description": meta("og:description") or meta("description", attr="name"),
            "image": meta("og:image"),
        }

    def _extract_primary_seller(self, html: str) -> dict | None:
        block = _extract_json_value(html, "merchantListing")
        if not isinstance(block, dict):
            return None
        merchant = block.get("merchant") or {}
        return {
            "id": merchant.get("id"),
            "name": merchant.get("name"),
            "score": _safe_score(merchant.get("sellerScore")),
            "isPrimary": True,
            "variants": [_normalize_variant(v) for v in (block.get("variants") or [])],
        }

    def _extract_other_sellers(self, html: str) -> list[dict]:
        block = _extract_json_value(html, "otherMerchants")
        if not isinstance(block, list):
            return []
        result = []
        for entry in block:
            if not isinstance(entry, dict):
                continue
            result.append({
                "id": entry.get("id"),
                "name": entry.get("name"),
                "score": _safe_score(entry.get("sellerScore")),
                "isPrimary": False,
                "variants": [_normalize_variant(v) for v in (entry.get("variants") or [])],
            })
        return result

    def _collect_size_index(self, primary: dict | None, others: list[dict]) -> list[str]:
        seen = []
        sources = []
        if primary:
            sources.append(primary)
        sources.extend(others)
        for seller in sources:
            for variant in seller.get("variants", []):
                size = variant.get("size")
                if size and size not in seen:
                    seen.append(size)
        return seen


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


def _normalize_variant(v: dict) -> dict:
    if not isinstance(v, dict):
        return {}
    price_block = v.get("price") or {}
    discounted = (price_block.get("discountedPrice") or {}).get("value")
    selling = (price_block.get("sellingPrice") or {}).get("value")
    original = (price_block.get("originalPrice") or {}).get("value")

    size = v.get("value") or v.get("attributeValue")
    if not size:
        for attr in v.get("variantAttributes") or []:
            if not isinstance(attr, dict):
                continue
            if attr.get("attributeType") == "Size" or attr.get("attributeName") in ("Beden", "Numara"):
                size = attr.get("attributeValue")
                break

    return {
        "size": size,
        "itemNumber": v.get("itemNumber"),
        "barcode": v.get("barcode"),
        "inStock": bool(v.get("inStock", True)),
        "isSelected": bool(v.get("isSelected", False)),
        "price": _to_float(discounted) or _to_float(selling) or _to_float(original),
        "originalPrice": _to_float(original),
        "currency": price_block.get("currency") or "TRY",
    }


def _safe_score(score) -> float | None:
    if isinstance(score, dict):
        return _to_float(score.get("value"))
    return _to_float(score)


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
