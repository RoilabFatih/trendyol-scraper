import json
import re
from urllib.parse import urlparse

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

    STATE_SCRIPT_PATTERN = re.compile(
        r"window\.__PRODUCT_DETAIL_APP_INITIAL_STATE__\s*=\s*(\{.*?\});",
        re.DOTALL,
    )

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)

    def fetch_product(self, url: str) -> dict:
        self._validate_url(url)

        try:
            response = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            raise ScraperError(f"Sayfaya ulaşılamadı: {exc}") from exc

        if response.status_code != 200:
            raise ScraperError(f"Trendyol HTTP {response.status_code} döndürdü.")

        product = self._parse_state(response.text)
        if product:
            return product

        return self._parse_html_fallback(response.text, url)

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

    def _parse_state(self, html: str) -> dict | None:
        match = self.STATE_SCRIPT_PATTERN.search(html)
        if not match:
            return None

        raw = match.group(1)
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            return None

        product = (
            state.get("product")
            or state.get("productDetail", {}).get("product")
            or {}
        )
        if not product:
            return None

        price_block = product.get("price") or {}
        brand = product.get("brand") or {}
        category = product.get("category") or {}
        images = product.get("images") or []
        rating = product.get("ratingScore") or {}

        return {
            "id": product.get("id"),
            "title": product.get("name"),
            "brand": brand.get("name"),
            "category": category.get("name"),
            "price": price_block.get("discountedPrice", {}).get("value")
            or price_block.get("sellingPrice", {}).get("value")
            or price_block.get("originalPrice", {}).get("value"),
            "originalPrice": price_block.get("originalPrice", {}).get("value"),
            "currency": price_block.get("currency") or "TRY",
            "rating": rating.get("averageRating"),
            "ratingCount": rating.get("totalRatingCount"),
            "url": product.get("url"),
            "images": [
                img if img.startswith("http") else f"https://cdn.dsmcdn.com{img}"
                for img in images[:8]
            ],
            "source": "state",
        }

    def _parse_html_fallback(self, html: str, url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")

        def meta(prop_name: str, attr: str = "property") -> str | None:
            tag = soup.find("meta", attrs={attr: prop_name})
            return tag.get("content") if tag and tag.get("content") else None

        title = (
            meta("og:title")
            or (soup.title.string.strip() if soup.title and soup.title.string else None)
        )
        description = meta("og:description") or meta("description", attr="name")
        image = meta("og:image")
        price = meta("product:price:amount")
        currency = meta("product:price:currency") or "TRY"

        if not title and not price:
            raise ScraperError(
                "Ürün verisi okunamadı. Sayfa yapısı değişmiş olabilir veya link bir ürün sayfası değil."
            )

        return {
            "title": title,
            "description": description,
            "price": float(price) if price else None,
            "currency": currency,
            "images": [image] if image else [],
            "url": url,
            "source": "meta",
        }
