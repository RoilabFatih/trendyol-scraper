# Trendyol Satıcı Paneli

Trendyol satıcı hesabınızdaki tüm ürünleri **Seller API** üzerinden çeken ve aynı zamanda satıcının **storefront** sayfasındaki tüm ürünleri tarayıp her iki veri setini tek panelde sunan bir Flask uygulaması. Railway'de tek tıkla yayına alınır.

## Sekmeler

### 1. Parametreler
- Satıcı ID (`mid`)
- Trendyol API Key & Secret
- API Base URL (opsiyonel, varsayılan `https://apigw.trendyol.com/integration`)
- API çağrısı başına sayfa boyutu

Tüm ayarlar SQLite'a (`data/app.db`) yazılır ve kalıcıdır. Railway'de kalıcılık istiyorsanız bir **Volume** bağlamanız gerekir (`/data` üzerine mount → `DATABASE_PATH=/data/app.db` env var).

### 2. İşlemler
- **Çalıştır** butonu bir background job başlatır
  1. **API aşaması:** `/integration/product/sellers/{sellerId}/products` sayfa sayfa çağrılır
  2. **Tarama aşaması:** `https://www.trendyol.com/sr?mid={sellerId}&pi={page}` sayfa sayfa scrape edilir
- Canlı ilerleme: API sayfa/toplam, ürün sayısı; tarama sayfa/toplam, ürün sayısı
- Canlı log akışı

### 3. Veriler
İki alt sekme:

**API Verileri**
| Ürün ID | Ana Ürün ID | Barkod | Model | Renk | Beden | Kategori | Marka | Satıcı Kodu | Stok | KDV | İlk Fiyat | İndirimli | Satışta? | Arşiv? | Kilitli? | Kilit Nedeni |

**Tarama Verileri** (Ana Ürün ID üzerinden API verileriyle JOIN edilir)
| Ana Ürün ID | Ürün Linki | Ürün Adı | Üstü Çizili | İndirimli | TY Plus | Favori | Yorum | Puan | Model | Renk | Kategori | Marka |

## API Endpoint'leri

| Endpoint | Açıklama |
|---|---|
| `GET /api/settings` | Mevcut ayarları döner (secret maskelenir) |
| `POST /api/settings` | Ayarları günceller |
| `POST /api/jobs/start` | Yeni iş başlatır |
| `GET /api/jobs/status?id=X&after_log_id=Y` | İş ilerlemesi + yeni loglar |
| `GET /api/data/api-products?limit=&offset=` | Kayıtlı API ürünleri |
| `GET /api/data/scraped-products?limit=&offset=` | Kayıtlı tarama ürünleri (API ile JOIN'li) |
| `POST /api/scrape` | (Eski) Tek ürün linki için varyant + satıcı karşılaştırma |
| `GET /healthz` | Health check |

## Yerel Çalıştırma

```bash
python -m venv .venv
.venv\Scripts\activate    # Windows
pip install -r requirements.txt
python app.py
```

`http://localhost:8000` → Parametreler sekmesinden başlayın.

## Railway

1. Repo'yu Railway'de New Project → GitHub Repo'dan deploy edin
2. Settings → Networking → Generate Domain
3. (Opsiyonel) Veriler kalıcı olsun isterse: Volumes → `/data` mount edin, ardından `DATABASE_PATH=/data/app.db` env var ekleyin

## Notlar

- Background job tek **gunicorn worker + 8 thread** ile çalışır (paylaşılan in-memory durum). İhtiyaç olursa harici queue (Celery/RQ) eklenebilir
- Storefront scraper Trendyol'un HTML değişikliklerine duyarlıdır; `storefront_scraper.py` içindeki JSON extraction güncellenebilir
- API kimlik bilgileri SQLite'a şifrelenmeden yazılır; production'da gerekirse `keyring` veya `cryptography` ile şifreleme eklenebilir
