---
name: meme-referans
description: Meme Referans — Kullanıcının attığı Instagram/TikTok videosunun (veya ekran görüntüsünün) "üstte beyaz şerit + yazı, altta film/dizi sahnesi" konseptinin YouTube Shorts'ta son 30 günde yapılıp yapılmadığını, abd-le-saat.vercel.app'teki kanallardan toplanan kapak arşiviyle karşılaştırarak bulur ve eşleşen videoların linkini verir. Kullanıcı bir video/reel/link atıp "bu yapılmış mı", "bunu yapan var mı", "referanslarda var mı", "meme referans", "YouTube'da var mı" dediğinde ya da /meme-referans yazdığında kullan.
---

# Meme Referans

Arşiv, `abd-le-saat.vercel.app` sitesinin takip ettiği kanallardan bu konsepti yapanların (her hafta yeniden
kontrol edilir) **son 30 gündeki bütün Shorts kapaklarıdır**. Videolar indirilmez, yalnızca kapaklar indirilir.
Arşiv ilk kurulumda 3 Kasım 2026'ya kadar dolduruldu; daha eskisi geriye dönük çekilmez, 30 günlük pencere
zamanla kendiliğinden dolar (3 Aralık 2026 civarında tam 30 gün olur). Raporda kapsamı söylerken
`status` çıktısındaki `oldest` tarihini kullan.

## Her ortamda çalışır (iki mod)

- **Ana bilgisayar (publisher):** arşivin sahibi olan tek Windows PC (`~/MemeReferans/publisher.json` var).
  Windows Görev Zamanlayıcı → `MemeReferans-Gunluk`, her gün 17:00 `update` çalıştırır: yeni kapakları indirir,
  okur, 30 günü geçenleri siler, sonra arşivi public GitHub deposuna yükler
  (`github.com/sinsnato97-collab/meme-referans-data`).
- **Yansı (mirror):** başka her bilgisayar ve cloud ortamı. Script `index.json` dosyasını GitHub'dan otomatik
  indirir (30 dakikada bir yeniler), kapakları gerektikçe indirir. Güncelleme yapmaz; `update/backfill/reocr/publish`
  burada reddedilir, bu normaldir. Arşivin tazeliği ana bilgisayarın açık olmasına bağlıdır.

Script: bu skillin klasöründeki `scripts/memeref.py` (yükleme sırasında verilen "Base directory" + `/scripts/memeref.py`).
Aşağıda `<script>` bu tam yol demektir. Python 3 ile `-I` kullanarak çalıştır: `python -I "<script>" ...`
(Linux/macOS'ta `python3`). `numpy`/`PIL` eksik diyorsa: `pip install numpy pillow`, sonra yeniden dene
(pip `--user` ile kurduysan `-I` olmadan çalıştır). Videodan kare çıkarmak için ffmpeg gerekir; yoksa
`pip install imageio-ffmpeg` yeterli. Link indirmek için `pip install yt-dlp`.

## Akış

1. **Durum:** `python -I "<script>" status`. Çıktıda `mode`, `ocr_engine`, `index_file`, `thumbnails`, `oldest`,
   `last_update` var.
   - `mode: publisher` ve `last_update` 26 saatten eskiyse önce `python -I "<script>" update` çalıştır
     (güncelleme + GitHub'a yükleme 2–5 dk sürer: komutu 10 dk zaman aşımıyla ya da arka planda çalıştır).
   - `mode: mirror` ve `last_update` 26 saatten eskiyse: güncelleme burada yapılamaz; raporda "arşiv en son
     <tarih> güncellendi (ana bilgisayar kapalı olabilir)" diye belirt ve devam et.
   - Arşiv indirilemezse (ağ hatası): ortamın ağ ayarı GitHub'ı engelliyor olabilir; kullanıcıya
     `raw.githubusercontent.com` ve `github.com` erişimine izin vermesini söyle.

2. **Girdi:**
   - Video dosyası (mp4/mov) ya da görsel → yolunu doğrudan ver.
   - Instagram/TikTok linki → script yt-dlp ile indirmeyi dener. Instagram giriş isterse ya da ortam siteye
     erişemezse başarısız olur; o zaman kullanıcıdan videoyu dosya olarak (ya da ekran görüntüsü) atmasını iste.
   - Yalnızca metin verildiyse → `--text "caption"`.

3. **Arama:** `python -I "<script>" search "<girdi>"` (ya da `search --text "..."`). Çıktı JSON:
   - `query_caption`: videonun üst yazısı (OCR). **OCR olmayan ortamlarda (`ocr_engine: none`, ör. Linux
     cloud) boş gelir ve `note` alanı bunu söyler:** `workdir/frames/` içinden birkaç kareyi Read ile aç,
     üstteki yazıyı kendin oku ve `search "<girdi>" --text "okuduğun yazı"` ile tekrar ara (girdiyi de ver ki
     sahne parmak izi de karşılaştırılsın). OCR yanlış/eksik okuduysa da aynısını yap.
   - `candidates`: yazı benzerliği (`text_score`) ve sahne benzerliği (`image_score`) en yüksek adaylar;
     her birinin `thumb` alanı diskteki kapak dosyasıdır.
   - `candidates_sheet`: sorgu karesi (kırmızı "SORGU") + numaralı aday kapakları tek görselde. **Read ile aç.**
   - `workdir/frames/`: videonun tamamına yayılmış ~30 kare (YouTube kapağı videonun herhangi bir
     anı olabilir; sahneyi tanımak için birkaçına bak). Girdi görselse burada tek kare (`f_001.jpg`) olur.

4. **Sahneyi tanı:** Videodaki alt sahnenin hangi film/dizi/animasyondan, hangi karakter/an olduğunu belirle.

5. **Anlamsal tarama (OCR/parmak izi yetmeyebilir):** Aynı espri farklı kelimelerle yazılmış olabilir.
   Esprinin anahtar kelimeleri, karakter/film adı ve eşanlamlılarıyla `status` çıktısındaki `index_file`
   içinde Grep yap (`"caption"` ve `"yt_title"` alanları). Bulduğun ek adayların kapaklarını
   `python -I "<script>" thumbs <id> <id> ...` ile al (yansıda indirir, yollarını yazar) ve Read ile aç.

6. **Karar:** Her güçlü aday kapağı sorgu karesiyle görsel olarak karşılaştır:
   - **Aynısı yapılmış:** aynı espri/durum metni (kelimeler farklı olabilir) + aynı ya da çok benzer sahne.
   - **Kısmen:** aynı espri farklı sahneyle, ya da aynı sahne farklı espriyle.
   - **Yok:** ikisi de yok.
   Puan rehberi (testlerden): `image_score` ≥ 0.80 → neredeyse kesin aynı sahne; 0.60–0.70 arası alakasız
   kapakların olağan gürültüsü. `text_score` nadir kelimeleri ağırlıklı sayar ve kök eşler
   (licence~license, sliming~slime): ≥ 0.6 → büyük olasılıkla aynı yazı; 0.25–0.6 → farklı kelimelerle
   aynı espri OLABİLİR — gerçek testte doğru eşleşme 0.27 ile 3. sıradaydı. Bu yüzden yazı puanına göre
   ilk 8 adayın hepsini mutlaka gözle kontrol et. Puana körü körüne güvenme; son karar gözle karşılaştırmadır.

7. **Rapor (Türkçe, kısa):**
   - Sonuç: Yapılmış / Kısmen / Bulunamadı.
   - Videodaki üst yazı ve sahnenin kaynağı (film/dizi).
   - Her eşleşme: kanal, tarih (`date_estimated: true` ise "~" koy, tahmini), YouTube linki
     (`https://www.youtube.com/shorts/<id>`), neden eşleştiği (bir cümle).
   - Bulunamadıysa: arşivin kapsamını söyle (kaç kapak, hangi tarih aralığı, kaç kanal).

## Bakım komutları (yalnızca ana bilgisayar)

- `status` → arşiv özeti (yansıda `status --sync` hemen yeniden indirir). `update --reclassify` → tüm
  kanalların konsept kontrolünü hemen yenile. `publish` → arşivi GitHub'a hemen yükle (normalde `update` yapar).
- `reocr` → bütün kapakların yazısını yeniden oku (ör. Windows'a İngilizce OCR paketi kurulduktan sonra).
- Kanal listesi her güncellemede siteden yenilenir. Konsepti yapan kanal: son 8 Shorts'unun en az yarısında
  üstte beyaz şerit + yazı olan kanal (çıkması için oranın %25'in altına düşmesi gerekir).
- Sorun olursa `~/MemeReferans/update.log` dosyasına bak.
- Skill değişirse: GitHub'daki `skill/` klasörü her yayında otomatik güncellenir; claude.ai'deki kopyanın da
  güncel kalması için skill zip'ini claude.ai → Ayarlar → Yetenekler → Skills'e yeniden yükle.
