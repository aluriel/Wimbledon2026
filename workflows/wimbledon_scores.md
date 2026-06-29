# Wimbledon 2026 Tenis Takip Sistemi

## Amaç
Wimbledon 2026 (29 Haz – 12 Tem) erkekler ve kadınlar tekler maçlarını canlı/final skorlarıyla yerel web sayfasında takip et. Maç saatleri TRT (UTC+3) olarak gösterilir.

## Girdiler
Yok. Turnuva sırasında istenen zaman çalıştırılır.

## Adımlar

### 1. Web sunucusunu başlat
```
python tools/serve_scores.py
```
- ESPN public API'den ATP ve WTA maç verilerini başlangıçta çeker
- Sadece Erkekler Tekler ve Kadınlar Tekler maçlarını gösterir
- http://localhost:5000 adresinde sayfayı yayınlar
- Sayfa açık kaldığı sürece terminali açık tut

### 2. Sayfayı aç
http://localhost:5000 adresine git.

- Tüm maçlar TRT saatiyle tarihe göre gruplandırılmış görünür
- Filtre düğmeleri: Tümü, Canlı, Bugün, Erkekler, Kadınlar
- Tur filtresi: 1T, 2T, 3T, 4T, ÇF, YF, Final
- Oyuncu adına göre arama desteği
- Tohumlamalar görünümü: ATP ve WTA tohumlamaları yan yana

### 3. Sunucuyu durdur
İşin bitince terminalde `Ctrl+C` bas.

## Bağımsız Çekme (opsiyonel)
```
python tools/fetch_scores.py
```
`.tmp/wimbledon_scores.json` dosyasına kaydeder ve bugünkü maçları terminale yazdırır.

## Kullanılan Araçlar
- `tools/serve_scores.py` — Flask web sunucusu, maç sayfasını yayınlar
- `tools/fetch_scores.py` — bağımsız çekme, JSON'ı .tmp/ dizinine kaydeder

## Veri Kaynağı
ESPN public API — API anahtarı gerekmez, ücretsiz:
- ATP: `https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard?dates=20260629-20260712`
- WTA: `https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard?dates=20260629-20260712`

## Beklenen Çıktı
http://localhost:5000 adresinde:
- Erkekler ve Kadınlar Tekler maçları tarihe göre gruplandırılmış (TRT)
- Canlı maçlar yanıp sönen "Canlı" rozeti ile
- Set skorları (ör. 2-1 büyük, "6-3 3-6 7-5" küçük detay)
- Oyuncu tohumlamaları kart üzerinde parantez içinde (ör. "(1)")
- Kort adı (Centre Court, Court 1 vb.) varsa gösterilir

## Kenar Durumlar

- **ESPN API boş döndürürse**: API geçici kapalı olabilir. Son önbellek verisi sunulmaya devam eder. Birkaç dakika bekleyip yenile.
- **ATP ve WTA aynı anda canlıysa**: Her iki endpoint ayrı çekilir, birleştirilir, zamana göre sıralanır.
- **Eleme maçları (22-25 Haz)**: URL tarihini `20260622` ile başlatarak kapsama dahil et.
- **5000 portu doluysa**: `serve_scores.py` son satırında portu değiştir (ör. `port=5001`).
- **Skor henüz gelmemişse**: Planlanmış maçlarda "— vs —" gösterilir; skor gelince otomatik güncellenir.

## Notlar
- TRT UTC+3 sabit ofset, DST yok. Turnuva boyunca değişmez.
- ESPN `date` alanı her zaman UTC ISO 8601 formatındadır ("Z" ile biter). +3 saat eklenerek TRT'ye çevrilir.
- Tenis maçlarında "home/away" kavramı yoktur; competitors listesindeki ilk oyuncu p1, ikincisi p2 olarak işlenir.
- Set skorları `linescores[].value` alanından (float) alınır, `int()`'e dönüştürülür.
- Tohum numarası `records[0].summary` alanından "(1)" formatında gelir; parantezler temizlenir.
