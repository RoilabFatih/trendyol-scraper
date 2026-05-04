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

## ⚠️ Storefront Tarama: TR Olmayan Sunucular Bloklanıyor

Trendyol'un `/sr?mid=...` (satıcı listeleme) sayfası **Cloudflare WAF** arkasında.
TR-dışı veri merkezi IP'lerinden gelen istekleri 403 ile reddediyor — Railway sunucusu da
bunlardan biri. **Seller API** (`apigw.trendyol.com`) ise her IP'den çalışır.

Bu yüzden iki çalıştırma seçeneği var:

| Senaryo | API çekimi | Storefront tarama |
|---|---|---|
| Panelden **Çalıştır** (Railway) | ✅ Çalışır | ❌ HTTP 403 |
| Yerel **`run_local.bat`** (Türk IP) | ✅ Çalışır | ✅ Çalışır (curl_cffi ile) |

### Yerel runner kullanımı (Windows tek tık)

1. Bu repoyu klonla: `git clone https://github.com/RoilabFatih/trendyol-scraper.git`
2. `local_config.example.json` dosyasını `local_config.json` olarak kopyala ve doldur:
   ```json
   {
     "panel_url": "https://web-production-f4cf6.up.railway.app",
     "access_token": "BURAYA_PANEL_ERIŞIM_TOKENI",
     "seller_id": "BURAYA_SATICI_ID",
     "api_key": "BURAYA_API_KEY",
     "api_secret": "BURAYA_API_SECRET",
     "page_size": 200,
     "scrape_max_pages": 200
   }
   ```
3. `run_local.bat` dosyasını çift tıkla
   - İlk çalıştırmada otomatik venv oluşturur ve bağımlılıkları yükler (curl_cffi dahil)
   - API'den ve storefront'tan veri çeker, sonuçları **Railway DB'ye gönderir**
   - Veriler: panel → Veriler sekmesi
4. Sonraki çalıştırmalarda sadece `run_local.bat` → her şey hazır

Sadece bir aşamayı çalıştırmak için:
```
run_local.bat --api-only
run_local.bat --scrape-only
```

## Railway

1. Repo'yu Railway'de New Project → GitHub Repo'dan deploy edin
2. Settings → Networking → Generate Domain
3. (Opsiyonel) Veriler kalıcı olsun isterse: Volumes → `/data` mount edin, ardından `DATABASE_PATH=/data/app.db` env var ekleyin

## Notlar

- Background job tek **gunicorn worker + 8 thread** ile çalışır (paylaşılan in-memory durum). İhtiyaç olursa harici queue (Celery/RQ) eklenebilir
- Storefront scraper Trendyol'un HTML değişikliklerine duyarlıdır; `storefront_scraper.py` içindeki JSON extraction güncellenebilir
- API kimlik bilgileri SQLite'a şifrelenmeden yazılır; production'da gerekirse `keyring` veya `cryptography` ile şifreleme eklenebilir
