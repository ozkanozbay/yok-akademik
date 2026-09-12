import os
import re
import time
import uuid
import threading
import urllib.parse
from typing import Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response

app = Flask(__name__)

BASE_URL = "https://akademik.yok.gov.tr/AkademikArama"
ROOT_URL = BASE_URL + "/"
REQUEST_DELAY = 0.8
MAX_DIRECT = 300

MAIN_FIELDS_FALLBACK = [
    "Eğitim Bilimleri Temel Alanı",
    "Fen Bilimleri ve Matematik",
    "Filoloji",
    "Güzel Sanatlar",
    "Hukuk",
    "İlahiyat",
    "Mimarlık, Planlama, Tasarım",
    "Mühendislik",
    "Sağlık Bilimleri",
    "Sosyal, Beşeri ve İdari Bilimler",
    "Spor Bilimleri",
    "Ziraat ve Orman ve Su Ürünleri",
]

JOBS: Dict[str, dict] = {}
CACHE: Dict[str, Tuple[float, object]] = {}
CACHE_TTL = 1800


def trnorm(value: str) -> str:
    if value is None:
        return ""
    table = str.maketrans({
        "I": "ı", "İ": "i", "Ş": "ş", "Ğ": "ğ", "Ü": "ü", "Ö": "ö", "Ç": "ç"
    })
    return " ".join(str(value).translate(table).lower().split())


def cache_get(key: str):
    item = CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > CACHE_TTL:
        CACHE.pop(key, None)
        return None
    return value


def cache_set(key: str, value):
    CACHE[key] = (time.time(), value)


class YokAcademicScraper:
    def __init__(self, delay: float = REQUEST_DELAY):
        self.delay = delay
        self.client = httpx.Client(
            timeout=httpx.Timeout(connect=20.0, read=120.0, write=30.0, pool=30.0),
            follow_redirects=True,
            verify=False,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7",
                "Referer": ROOT_URL,
            },
        )

    def close(self):
        self.client.close()

    def _sleep(self):
        time.sleep(self.delay)

    def _get(self, url: str):
        last_error = None
        for attempt in range(3):
            try:
                r = self.client.get(url)
                r.raise_for_status()
                return r
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as e:
                last_error = e
                if attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
        raise last_error

    def _request_soup(self, url: str):
        self._sleep()
        r = self._get(url)
        return r, BeautifulSoup(r.text, "lxml")

    @staticmethod
    def _join(current_url: str, href: str) -> str:
        return urllib.parse.urljoin(current_url, href)

    @staticmethod
    def _anchor_text(a) -> str:
        return " ".join(a.get_text(" ", strip=True).split())

    def _find_anchor(self, soup: BeautifulSoup, text: str, current_url: str,
                     require_islem: bool = False,
                     keyword_link: Optional[bool] = None) -> Optional[str]:
        wanted = trnorm(text)
        candidates = []
        for a in soup.find_all("a", href=True):
            label = self._anchor_text(a)
            if trnorm(label) != wanted:
                continue
            href = a.get("href", "")
            cls = " ".join(a.get("class", []))
            if require_islem and "islem=" not in href:
                continue
            is_kw = "anahtarKelime" in cls or "anahtarkelime" in trnorm(cls)
            if keyword_link is True and not is_kw:
                continue
            if keyword_link is False and is_kw:
                continue
            candidates.append(self._join(current_url, href))
        return candidates[0] if candidates else None

    def main_fields(self) -> List[str]:
        r, soup = self._request_soup(ROOT_URL)
        found = []
        page_text = trnorm(soup.get_text(" ", strip=True))
        for field in MAIN_FIELDS_FALLBACK:
            if trnorm(field) in page_text:
                found.append(field)
        return found or MAIN_FIELDS_FALLBACK

    def resolve_filter(self, main: str, sub: str = "", keyword: str = ""):
        r, soup = self._request_soup(ROOT_URL)
        url = self._find_anchor(soup, main, r.url.__str__())
        if not url:
            raise RuntimeError(f"Temel alan YÖK sayfasında bulunamadı: {main}")

        r, soup = self._request_soup(url)
        current = str(r.url)

        if sub:
            sub_url = self._find_anchor(soup, sub, current, require_islem=True, keyword_link=False)
            if not sub_url:
                # Sonuç rozeti aynı filtreye götürebildiğinden daha gevşek ikinci deneme
                sub_url = self._find_anchor(soup, sub, current)
            if not sub_url:
                raise RuntimeError(f"Bilim/Sanat alanı bulunamadı: {sub}")
            r, soup = self._request_soup(sub_url)
            current = str(r.url)

        if keyword:
            kw_url = self._find_anchor(soup, keyword, current, require_islem=True, keyword_link=True)
            if not kw_url:
                kw_url = self._find_anchor(soup, keyword, current, keyword_link=True)
            if not kw_url:
                # Bazı YÖK sayfalarında sınıf adı değişebiliyor
                kw_url = self._find_anchor(soup, keyword, current)
            if not kw_url:
                raise RuntimeError(f"Anahtar kelime bulunamadı: {keyword}")
            r, soup = self._request_soup(kw_url)
            current = str(r.url)

        return current, r, soup

    def _panel_links(self, soup: BeautifulSoup, heading_words: List[str]) -> List[str]:
        results = []
        for panel in soup.select(".panel"):
            heading = panel.select_one(".panel-heading")
            heading_text = trnorm(heading.get_text(" ", strip=True) if heading else "")
            if not any(word in heading_text for word in heading_words):
                continue
            for a in panel.select("a[href]"):
                txt = self._anchor_text(a)
                if txt and trnorm(txt) not in {"tümü", "tum", "daha fazla", "filtreyi kaldır"}:
                    results.append(txt)
        return sorted(set(results), key=lambda x: trnorm(x))

    def subfields(self, main: str) -> List[str]:
        url, r, soup = self.resolve_filter(main)
        values = self._panel_links(soup, ["bilim", "sanat"])
        if values:
            return [x for x in values if x not in MAIN_FIELDS_FALLBACK]

        # DOM değişirse yedek yöntem
        out = []
        for a in soup.select("a[href*='islem=']"):
            cls = " ".join(a.get("class", []))
            if "anahtarKelime" in cls:
                continue
            txt = self._anchor_text(a)
            if not txt or txt in MAIN_FIELDS_FALLBACK:
                continue
            if re.fullmatch(r"[\d»›><]+", txt):
                continue
            if trnorm(txt) in {"filtrele", "filtreyi kaldır", "aktar", "tümü"}:
                continue
            out.append(txt)
        return sorted(set(out), key=lambda x: trnorm(x))

    def keywords(self, main: str, sub: str) -> List[str]:
        url, r, soup = self.resolve_filter(main, sub)
        values = self._panel_links(soup, ["anahtar"])
        if values:
            return values
        out = []
        for a in soup.find_all("a", href=True):
            cls = " ".join(a.get("class", []))
            if "anahtarKelime" not in cls and "anahtarkelime" not in trnorm(cls):
                continue
            txt = self._anchor_text(a)
            if txt:
                out.append(txt)
        return sorted(set(out), key=lambda x: trnorm(x))

    def universities(self, main: str, sub: str = "", keyword: str = "") -> List[dict]:
        url, r, soup = self.resolve_filter(main, sub, keyword)
        return self._read_universities(soup)

    @staticmethod
    def _read_universities(soup: BeautifulSoup) -> List[dict]:
        out = []
        for cb in soup.select("input[name^='secZ'][type='checkbox']"):
            name = cb.get("name", "")
            value = cb.get("value", "")
            li = cb.find_parent("li")
            label = " ".join((li.get_text(" ", strip=True) if li else "").split())
            m = re.search(r"\((\d+)\)\s*$", label)
            count = int(m.group(1)) if m else 0
            clean = re.sub(r"\s*\(\d+\)\s*$", "", label).strip()
            if clean:
                out.append({"name": clean, "count": count, "checkbox_name": name, "checkbox_value": value})
        out.sort(key=lambda x: trnorm(x["name"]))
        return out

    def _parse_rows(self, soup: BeautifulSoup) -> List[dict]:
        data = []
        for row in soup.select("tr[id^='authorInfo_']"):
            row_id = row.get("id", "")
            author_id = row_id.replace("authorInfo_", "").strip()
            name_el = row.select_one("h4 a")
            name = name_el.get_text(" ", strip=True) if name_el else ""
            href = name_el.get("href", "") if name_el else ""
            profile_url = urllib.parse.urljoin(BASE_URL + "/", href) if href else ""
            h6s = row.select("h6")
            title = h6s[0].get_text(" ", strip=True) if h6s else ""
            path = h6s[1].get_text(" ", strip=True) if len(h6s) > 1 else ""
            parts = [x.strip() for x in path.split("/") if x.strip()]
            main_el = row.select_one(".label-success a, .label-success")
            sub_el = row.select_one(".label-primary a, .label-primary")
            keywords = []
            for a in row.select("span:not(.label) a"):
                txt = a.get_text(" ", strip=True)
                if txt and txt not in keywords:
                    keywords.append(txt)
            email_el = row.select_one("a[href^='mailto:']")
            email = email_el.get("href", "").replace("mailto:", "").strip() if email_el else ""
            data.append({
                "id": author_id,
                "isim": name,
                "unvan": title,
                "universite": parts[0] if len(parts) > 0 else "",
                "fakulte": parts[1] if len(parts) > 1 else "",
                "bolum": parts[2] if len(parts) > 2 else "",
                "anabilim_dali": parts[3] if len(parts) > 3 else "",
                "temel_alan": main_el.get_text(" ", strip=True) if main_el else "",
                "bilim_alani": sub_el.get_text(" ", strip=True) if sub_el else "",
                "uzmanlik_alanlari": " ; ".join(keywords),
                "email": email,
                "profil_url": profile_url,
            })
        return data

    def _next_url(self, soup: BeautifulSoup, current_url: str, page_num: int) -> Optional[str]:
        links = soup.select("a[href*='AramaFiltrele']")
        for a in links:
            txt = a.get_text(" ", strip=True)
            if txt in {">", "»", "›", "Sonraki"}:
                return self._join(current_url, a.get("href", ""))
        for a in links:
            txt = a.get_text(" ", strip=True)
            if txt.isdigit() and int(txt) == page_num + 1:
                return self._join(current_url, a.get("href", ""))
        return None

    def _scrape_pages(self, first_response, max_records: int = 10000) -> List[dict]:
        all_data = []
        seen = set()
        response = first_response
        page_num = 1
        while response is not None and len(all_data) < max_records:
            soup = BeautifulSoup(response.text, "lxml")
            rows = self._parse_rows(soup)
            new_rows = []
            for item in rows:
                key = item["id"] or item["profil_url"] or (item["isim"] + "|" + item["universite"])
                if key and key not in seen:
                    seen.add(key)
                    new_rows.append(item)
            all_data.extend(new_rows)
            if not rows or not new_rows:
                break
            next_url = self._next_url(soup, str(response.url), page_num)
            if not next_url:
                break
            self._sleep()
            try:
                response = self._get(next_url)
            except Exception:
                break
            page_num += 1
        return all_data[:max_records]

    def _submit_university(self, field_url: str, checkbox_name: str, checkbox_value: str):
        self._sleep()
        r = self._get(field_url)
        soup = BeautifulSoup(r.text, "lxml")
        cb = soup.find("input", attrs={"name": checkbox_name})
        if not cb:
            return None
        form = cb.find_parent("form")
        if not form:
            return None

        payload = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            typ = (inp.get("type") or "text").lower()
            if typ in {"submit", "button", "checkbox", "radio"}:
                continue
            payload[name] = inp.get("value", "")
        payload[checkbox_name] = checkbox_value
        btn = form.find("button", attrs={"name": "kaydet"}) or form.find("input", attrs={"name": "kaydet"})
        if btn:
            payload["kaydet"] = btn.get("value", "")
        else:
            payload["kaydet"] = ""

        action = urllib.parse.urljoin(str(r.url), form.get("action") or str(r.url))
        method = (form.get("method") or "get").lower()
        self._sleep()
        if method == "post":
            resp = self.client.post(action, data=payload)
        else:
            resp = self.client.get(action, params=payload)
        resp.raise_for_status()
        return resp

    def _fallback_query(self, field_text: str, university: str) -> List[dict]:
        q = f"{university} {field_text}".strip()
        url = f"{BASE_URL}/AkademisyenArama?islem=arama&q={urllib.parse.quote(q)}"
        self._sleep()
        r = self._get(url)
        return self._scrape_pages(r, max_records=500)

    def scrape(self, main: str, sub: str = "", keyword: str = "", university: str = "",
               title: str = "", progress=None) -> List[dict]:
        field_url, r, soup = self.resolve_filter(main, sub, keyword)
        unis = self._read_universities(soup)
        positive = [u for u in unis if u["count"] > 0]
        expected = sum(u["count"] for u in positive)

        if progress:
            progress(3, f"YÖK filtresi açıldı. Yaklaşık {expected} kayıt görünüyor.")

        all_data = []
        if university:
            selected = [u for u in positive if trnorm(u["name"]) == trnorm(university)]
            if selected:
                u = selected[0]
                resp = self._submit_university(field_url, u["checkbox_name"], u["checkbox_value"])
                if resp is not None:
                    all_data = self._scrape_pages(resp, max_records=max(u["count"] + 30, 300))
                if not all_data and u["count"] > 0:
                    all_data = self._fallback_query(keyword or sub or main, u["name"])
            else:
                all_data = self._fallback_query(keyword or sub or main, university)
        elif expected and expected <= MAX_DIRECT:
            all_data = self._scrape_pages(r, max_records=MAX_DIRECT + 50)
        elif positive:
            total_unis = len(positive)
            for idx, u in enumerate(positive, start=1):
                pct = 5 + int((idx - 1) / max(total_unis, 1) * 88)
                if progress:
                    progress(pct, f"{idx}/{total_unis}: {u['name']} taranıyor…")
                try:
                    resp = self._submit_university(field_url, u["checkbox_name"], u["checkbox_value"])
                    part = self._scrape_pages(resp, max_records=max(u["count"] + 30, 300)) if resp is not None else []
                    if not part and u["count"] > 0:
                        part = self._fallback_query(keyword or sub or main, u["name"])
                    all_data.extend(part)
                except Exception:
                    try:
                        all_data.extend(self._fallback_query(keyword or sub or main, u["name"]))
                    except Exception:
                        pass
        else:
            all_data = self._scrape_pages(r, max_records=MAX_DIRECT + 50)

        # Tekilleştir + seçilen filtreleri kesin olarak uygula
        filtered = []
        seen = set()
        for x in all_data:
            key = x.get("id") or x.get("profil_url") or (x.get("isim", "") + "|" + x.get("universite", ""))
            if key in seen:
                continue
            seen.add(key)
            if main and trnorm(x.get("temel_alan", "")) != trnorm(main):
                continue
            if sub and trnorm(x.get("bilim_alani", "")) != trnorm(sub):
                continue
            if keyword and trnorm(keyword) not in trnorm(x.get("uzmanlik_alanlari", "")):
                continue
            if university and trnorm(x.get("universite", "")) != trnorm(university):
                continue
            if title and trnorm(title) not in trnorm(x.get("unvan", "")):
                continue
            filtered.append(x)

        filtered.sort(key=lambda x: (trnorm(x.get("universite", "")), trnorm(x.get("unvan", "")), trnorm(x.get("isim", ""))))
        if progress:
            progress(100, f"Tamamlandı: {len(filtered)} akademisyen bulundu.")
        return filtered


HTML = r'''<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YÖK Akademik Alan Tarama Sistemi</title>
<style>
*{box-sizing:border-box} body{margin:0;font-family:Arial,Helvetica,sans-serif;background:#f4f6f9;color:#1f2937}
.header{background:linear-gradient(135deg,#123a63,#1f5d96);color:#fff;padding:28px 20px;text-align:center}
.header h1{margin:0 0 7px;font-size:28px}.header p{margin:0;opacity:.88;font-size:14px}
.container{max-width:1450px;margin:24px auto;padding:0 18px}.card{background:#fff;border-radius:12px;padding:22px;box-shadow:0 2px 12px rgba(0,0,0,.08);margin-bottom:20px}
.card-title{font-size:18px;font-weight:700;margin-bottom:18px;color:#123a63}.filters{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.form-group{display:flex;flex-direction:column}label{font-size:13px;font-weight:700;margin-bottom:7px;color:#475569}
select,input{width:100%;padding:12px;border:1px solid #cbd5e1;border-radius:7px;background:#fff;font-size:14px;outline:none}
select:focus,input:focus{border-color:#1f5d96;box-shadow:0 0 0 2px rgba(31,93,150,.12)}
.buttons{margin-top:20px;display:flex;gap:10px;flex-wrap:wrap}button{border:0;padding:12px 18px;border-radius:7px;cursor:pointer;font-size:14px;font-weight:700}.btn-search{background:#1769aa;color:#fff}.btn-clear{background:#e9edf2}.btn-export{background:#198754;color:#fff;margin-left:auto}
.status{margin-top:15px;padding:12px;border-radius:7px;background:#eef5fb;color:#174e7b;font-size:13px}.progress{height:9px;background:#dbe5ee;border-radius:99px;overflow:hidden;margin-top:10px;display:none}.progress>div{height:100%;width:0;background:#1769aa;transition:width .3s}
.summary{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}.result-count{font-size:16px;font-weight:700;color:#123a63}.search-box{width:330px}.table-wrap{overflow:auto;max-height:70vh}table{width:100%;border-collapse:collapse;font-size:13px}thead{background:#123a63;color:#fff;position:sticky;top:0;z-index:1}th{padding:11px 9px;text-align:left;white-space:nowrap}td{padding:10px 9px;border-bottom:1px solid #e5e7eb;vertical-align:top}tbody tr:hover{background:#f8fafc}.name{font-weight:700;color:#1769aa}.badge{display:inline-block;background:#e8f2fb;color:#1769aa;border-radius:15px;padding:4px 8px;font-size:11px;font-weight:700}.profile-btn{display:inline-block;padding:6px 9px;background:#1769aa;color:#fff;border-radius:5px;text-decoration:none;white-space:nowrap}.empty{text-align:center;color:#64748b;padding:45px}.muted{color:#64748b;font-size:12px;margin-top:7px}
@media(max-width:900px){.filters{grid-template-columns:1fr}.search-box{width:100%}.btn-export{margin-left:0}}
</style>
</head>
<body>
<div class="header"><h1>YÖK Akademik Alan Tarama Sistemi</h1><p>YÖK Akademik canlı verisi • Temel Alan → Bilim/Sanat Alanı → Anahtar Kelime</p></div>
<div class="container">
<div class="card">
<div class="card-title">Akademik Alan Filtresi</div>
<div class="filters">
<div class="form-group"><label>Temel Alan</label><select id="main"><option value="">Yükleniyor…</option></select></div>
<div class="form-group"><label>Bilim / Sanat Alanı</label><select id="sub" disabled><option value="">Önce temel alan seçiniz</option></select></div>
<div class="form-group"><label>Anahtar Kelime / Uzmanlık Alanı</label><select id="keyword" disabled><option value="">Tümü</option></select></div>
<div class="form-group"><label>Akademik Unvan</label><select id="title"><option value="">Tüm Unvanlar</option><option>PROFESÖR</option><option>DOÇENT</option><option>DOKTOR ÖĞRETİM ÜYESİ</option><option>ÖĞRETİM GÖREVLİSİ</option><option>ARAŞTIRMA GÖREVLİSİ</option></select></div>
<div class="form-group"><label>Üniversite</label><select id="university" disabled><option value="">Tüm Üniversiteler</option></select></div>
<div class="form-group"><label>Sonuçlarda Serbest Arama</label><input id="localSearch" placeholder="Ad, üniversite, fakülte, bölüm…"></div>
</div>
<div class="buttons"><button class="btn-search" id="searchBtn">Akademisyenleri Bul</button><button class="btn-clear" id="clearBtn">Temizle</button><button class="btn-export" id="exportBtn">Excel / CSV Aktar</button></div>
<div class="status" id="status">YÖK Akademik bağlantısı hazırlanıyor…</div><div class="progress" id="progress"><div id="progressBar"></div></div>
<div class="muted">Geniş alanlarda YÖK'ün sonuç sınırını aşmak için üniversiteler ayrı ayrı taranıp sonuçlar birleştirilir.</div>
</div>
<div class="card">
<div class="summary"><div class="result-count" id="count">0 akademisyen</div><input class="search-box" id="tableSearch" placeholder="Tablo içinde ara…"></div>
<div class="table-wrap"><table><thead><tr><th>#</th><th>Unvan</th><th>Akademisyen</th><th>Üniversite</th><th>Fakülte</th><th>Bölüm</th><th>ABD</th><th>Temel Alan</th><th>Bilim Alanı</th><th>Uzmanlık</th><th>E-posta</th><th>Profil</th></tr></thead><tbody id="body"><tr><td colspan="12" class="empty">Henüz arama yapılmadı.</td></tr></tbody></table></div>
</div></div>
<script>
let results=[];
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
async function getJSON(url){const r=await fetch(url);const j=await r.json();if(!r.ok)throw new Error(j.error||'İstek başarısız');return j}
async function loadMain(){try{const j=await getJSON('/api/main-fields');$('main').innerHTML='<option value="">Temel alan seçiniz</option>'+j.items.map(x=>`<option>${esc(x)}</option>`).join('');$('status').textContent='Hazır. Bir temel alan seçiniz.'}catch(e){$('status').textContent='Hata: '+e.message}}
$('main').addEventListener('change',async()=>{const main=$('main').value;$('sub').disabled=true;$('keyword').disabled=true;$('university').disabled=true;$('sub').innerHTML='<option>Yükleniyor…</option>';$('keyword').innerHTML='<option value="">Tümü</option>';$('university').innerHTML='<option value="">Bilim alanı seçildikten sonra yüklenecek</option>';if(!main)return;try{$('status').textContent='Bilim/Sanat alanları YÖK Akademik’ten okunuyor… Bu işlem 30–120 saniye sürebilir.';const j=await getJSON('/api/subfields?main='+encodeURIComponent(main));$('sub').innerHTML='<option value="">Tüm Bilim/Sanat Alanları</option>'+j.items.map(x=>`<option>${esc(x)}</option>`).join('');$('sub').disabled=false;$('status').textContent='Bilim/Sanat alanı hazır. Bir alan seçiniz.'}catch(e){$('status').textContent='Hata: '+e.message}});
$('sub').addEventListener('change',async()=>{const main=$('main').value,sub=$('sub').value;$('keyword').innerHTML='<option value="">Tümü</option>';if(sub){try{$('status').textContent='Anahtar kelimeler YÖK Akademik’ten okunuyor…';const j=await getJSON('/api/keywords?main='+encodeURIComponent(main)+'&sub='+encodeURIComponent(sub));$('keyword').innerHTML='<option value="">Tüm Anahtar Kelimeler</option>'+j.items.map(x=>`<option>${esc(x)}</option>`).join('');$('keyword').disabled=false}catch(e){$('status').textContent='Anahtar kelime listesi alınamadı: '+e.message}}else{$('keyword').disabled=true}await loadUniversities()});
$('keyword').addEventListener('change',loadUniversities);
async function loadUniversities(){const main=$('main').value;if(!main)return;const q=new URLSearchParams({main,sub:$('sub').value,keyword:$('keyword').value});try{const j=await getJSON('/api/universities?'+q.toString());$('university').innerHTML='<option value="">Tüm Üniversiteler</option>'+j.items.map(x=>`<option value="${esc(x.name)}">${esc(x.name)} (${x.count})</option>`).join('');$('university').disabled=false;$('status').textContent=`Hazır. Bu filtrede ${j.total} kayıt YÖK tarafından bildiriliyor.`}catch(e){$('status').textContent='Üniversite listesi alınamadı: '+e.message}}
$('searchBtn').addEventListener('click',async()=>{if(!$('main').value){alert('Önce temel alan seçiniz.');return}const payload={main:$('main').value,sub:$('sub').value,keyword:$('keyword').value,title:$('title').value,university:$('university').value};$('searchBtn').disabled=true;$('progress').style.display='block';$('progressBar').style.width='1%';$('status').textContent='Tarama başlatılıyor…';$('body').innerHTML='<tr><td colspan="12" class="empty">YÖK Akademik taranıyor…</td></tr>';try{const r=await fetch('/api/search/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const j=await r.json();if(!r.ok)throw new Error(j.error||'Arama başlatılamadı');poll(j.job_id)}catch(e){$('searchBtn').disabled=false;$('status').textContent='Hata: '+e.message}});
async function poll(id){try{const j=await getJSON('/api/search/status/'+id);$('progressBar').style.width=(j.progress||0)+'%';$('status').textContent=j.message||'Taranıyor…';if(j.status==='done'){results=j.results||[];$('searchBtn').disabled=false;render(results);return}if(j.status==='error'){throw new Error(j.error||'Tarama başarısız')}setTimeout(()=>poll(id),1000)}catch(e){$('searchBtn').disabled=false;$('status').textContent='Hata: '+e.message}}
function render(arr){$('count').textContent=arr.length+' akademisyen';if(!arr.length){$('body').innerHTML='<tr><td colspan="12" class="empty">Kriterlere uygun akademisyen bulunamadı.</td></tr>';return}$('body').innerHTML=arr.map((x,i)=>`<tr><td>${i+1}</td><td><span class="badge">${esc(x.unvan)}</span></td><td class="name">${esc(x.isim)}</td><td>${esc(x.universite)}</td><td>${esc(x.fakulte)}</td><td>${esc(x.bolum)}</td><td>${esc(x.anabilim_dali)}</td><td>${esc(x.temel_alan)}</td><td>${esc(x.bilim_alani)}</td><td>${esc(x.uzmanlik_alanlari)}</td><td>${esc(x.email)}</td><td>${x.profil_url?`<a class="profile-btn" target="_blank" rel="noopener" href="${esc(x.profil_url)}">YÖK Profili</a>`:''}</td></tr>`).join('')}
function localFilter(){const q=($('tableSearch').value+' '+$('localSearch').value).toLocaleLowerCase('tr-TR').trim();if(!q){render(results);return}render(results.filter(x=>Object.values(x).join(' ').toLocaleLowerCase('tr-TR').includes(q)))}
$('tableSearch').addEventListener('input',localFilter);$('localSearch').addEventListener('input',localFilter);
$('clearBtn').addEventListener('click',()=>{location.reload()});
$('exportBtn').addEventListener('click',()=>{if(!results.length){alert('Aktarılacak veri yok.');return}const headers=['Unvan','Akademisyen','Üniversite','Fakülte','Bölüm','ABD','Temel Alan','Bilim Alanı','Uzmanlık','E-posta','Profil'];const fields=['unvan','isim','universite','fakulte','bolum','anabilim_dali','temel_alan','bilim_alani','uzmanlik_alanlari','email','profil_url'];const q=v=>'"'+String(v??'').replaceAll('"','""')+'"';let csv='\uFEFF'+headers.map(q).join(';')+'\n';for(const x of results)csv+=fields.map(f=>q(x[f])).join(';')+'\n';const blob=new Blob([csv],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='yok-akademik-sonuclari.csv';a.click();URL.revokeObjectURL(url)});
loadMain();
</script></body></html>'''


@app.get("/")
def index():
    return Response(HTML, mimetype="text/html")


@app.get("/api/main-fields")
def api_main_fields():
    key = "main-fields"
    cached = cache_get(key)
    if cached is not None:
        return jsonify({"items": cached})
    scraper = YokAcademicScraper()
    try:
        items = scraper.main_fields()
        cache_set(key, items)
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"items": MAIN_FIELDS_FALLBACK, "warning": str(e)})
    finally:
        scraper.close()


@app.get("/api/subfields")
def api_subfields():
    main = request.args.get("main", "").strip()
    if not main:
        return jsonify({"error": "main gerekli"}), 400
    key = f"sub::{main}"
    cached = cache_get(key)
    if cached is not None:
        return jsonify({"items": cached})
    scraper = YokAcademicScraper()
    try:
        items = scraper.subfields(main)
        cache_set(key, items)
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    finally:
        scraper.close()


@app.get("/api/keywords")
def api_keywords():
    main = request.args.get("main", "").strip()
    sub = request.args.get("sub", "").strip()
    if not main or not sub:
        return jsonify({"error": "main ve sub gerekli"}), 400
    key = f"kw::{main}::{sub}"
    cached = cache_get(key)
    if cached is not None:
        return jsonify({"items": cached})
    scraper = YokAcademicScraper()
    try:
        items = scraper.keywords(main, sub)
        cache_set(key, items)
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    finally:
        scraper.close()


@app.get("/api/universities")
def api_universities():
    main = request.args.get("main", "").strip()
    sub = request.args.get("sub", "").strip()
    keyword = request.args.get("keyword", "").strip()
    if not main:
        return jsonify({"error": "main gerekli"}), 400
    key = f"uni::{main}::{sub}::{keyword}"
    cached = cache_get(key)
    if cached is not None:
        return jsonify({"items": cached, "total": sum(x["count"] for x in cached)})
    scraper = YokAcademicScraper()
    try:
        items = scraper.universities(main, sub, keyword)
        cache_set(key, items)
        return jsonify({"items": items, "total": sum(x["count"] for x in items)})
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    finally:
        scraper.close()


def run_job(job_id: str, params: dict):
    def progress(pct, message):
        JOBS[job_id].update({"progress": pct, "message": message})

    scraper = YokAcademicScraper()
    try:
        data = scraper.scrape(
            main=params.get("main", ""),
            sub=params.get("sub", ""),
            keyword=params.get("keyword", ""),
            university=params.get("university", ""),
            title=params.get("title", ""),
            progress=progress,
        )
        JOBS[job_id].update({"status": "done", "progress": 100, "message": f"Tamamlandı: {len(data)} akademisyen bulundu.", "results": data})
    except Exception as e:
        JOBS[job_id].update({"status": "error", "error": str(e), "message": "Tarama sırasında hata oluştu."})
    finally:
        scraper.close()


@app.post("/api/search/start")
def api_search_start():
    params = request.get_json(silent=True) or {}
    if not params.get("main"):
        return jsonify({"error": "Temel alan seçilmelidir."}), 400
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "running", "progress": 1, "message": "YÖK Akademik bağlantısı kuruluyor…", "results": []}
    threading.Thread(target=run_job, args=(job_id, params), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.get("/api/search/status/<job_id>")
def api_search_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Arama bulunamadı."}), 404
    return jsonify(job)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
