# Trendyol Ürün Bilgisi Çekici

Trendyol ürün linki yapıştırıldığında ürünün adı, fiyatı, markası, görselleri ve puanını gösteren basit bir Flask web uygulaması. Railway üzerinde dağıtım için hazırdır.

## Özellikler

- Tek sayfalık modern web arayüzü
- `__PRODUCT_DETAIL_APP_INITIAL_STATE__` JSON state'inden veri çıkarma (öncelikli)
- HTML meta etiketlerinden fallback parser
- `/api/scrape` JSON API endpoint'i
- `/healthz` health check endpoint'i

## Yerel Çalıştırma

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Tarayıcıda `http://localhost:8000` adresini açın.

## API Kullanımı

```bash
curl -X POST http://localhost:8000/api/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.trendyol.com/marka/urun-adi-p-12345678"}'
```

## GitHub'a Yükleme

```bash
cd D:\ClaudeProje
git init
git add .
git commit -m "Initial commit: Trendyol scraper"
git branch -M main
git remote add origin https://github.com/<KULLANICI>/trendyol-scraper.git
git push -u origin main
```

## Railway'e Deploy

### Yöntem 1: Railway Dashboard (en kolay)
1. https://railway.app/new sayfasına gidin
2. **Deploy from GitHub repo** seçin
3. Bu repoyu seçin
4. Railway otomatik olarak `railway.json` dosyasını okur, Nixpacks ile Python projesini tespit eder ve `gunicorn` ile başlatır
5. Deploy bitince **Settings → Networking → Generate Domain** ile public URL alın

### Yöntem 2: Railway CLI
```bash
npm i -g @railway/cli
railway login
railway init
railway up
railway domain
```

## Dosya Yapısı

```
.
├── app.py              # Flask uygulaması ve route'lar
├── scraper.py          # Trendyol parser (state + meta fallback)
├── templates/
│   └── index.html      # Web arayüzü
├── requirements.txt    # Python bağımlılıkları
├── Procfile            # Railway/Heroku başlatma komutu
├── railway.json        # Railway dağıtım yapılandırması
├── runtime.txt         # Python versiyonu
└── .gitignore
```

## Notlar

- Trendyol HTML yapısını değiştirirse `scraper.py` içindeki regex/parser güncellenmeli
- Yoğun istekte Trendyol IP'yi rate-limit edebilir; gerekirse proxy / cache ekleyin
- `gunicorn` 2 worker ile çalışır; trafik artarsa `Procfile` ve `railway.json` içinde arttırın
