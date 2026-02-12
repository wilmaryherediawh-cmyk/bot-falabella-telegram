import os
import re
import json
import time
import random
import logging
import html as html_lib
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests

# =========================
# Config
# =========================
BASE = "https://www.plazavea.com.pe"

DEFAULT_CATEGORY_URLS = [
    "https://www.plazavea.com.pe/supermercado",
    "https://www.plazavea.com.pe/tecnologia",
    "https://www.plazavea.com.pe/electrohogar",
]

ENV_CATEGORY_URLS = os.getenv("CATEGORY_URLS", "").strip()
MIN_DISCOUNT_PERCENT = int(os.getenv("MIN_DISCOUNT_PERCENT", "30"))
MAX_PAGES_PER_CATEGORY = int(os.getenv("MAX_PAGES_PER_CATEGORY", "2"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "35"))
STATE_FILE = "state.json"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

SLEEP_MIN = float(os.getenv("SLEEP_MIN", "2.0"))
SLEEP_MAX = float(os.getenv("SLEEP_MAX", "4.5"))

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("plazavea_bot")

# =========================
# Helpers
# =========================
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Referer": BASE + "/",
    })
    return s

def sleep_a_bit() -> None:
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

def looks_blocked(html: str) -> bool:
    h = (html or "").lower()
    return any(x in h for x in ["captcha", "access denied", "unusual traffic", "verify you are human", "cloudflare"])

def with_page_param(url: str, page: int) -> str:
    # Muchos sitios aceptan ?page=2. Si no afecta, no rompe nada.
    p = urlparse(url)
    q = parse_qs(p.query)
    q["page"] = [str(page)]
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q, doseq=True), p.fragment))

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"sent": {}, "last_run": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("sent", {}), dict):
            # si estaba mal, lo reparamos
            data["sent"] = {}
        return data
    except Exception:
        return {"sent": {}, "last_run": None}

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# =========================
# Telegram
# =========================
def telegram_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Faltan BOT_TOKEN o CHAT_ID en Secrets (Settings → Secrets and variables → Actions).")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False, "parse_mode": "HTML"}
    r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")

# =========================
# Parsing utilities
# =========================
def _to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        # quita símbolos comunes
        s = s.replace("S/", "").replace("s/", "").strip()
        # normaliza 1.234,56 -> 1234.56
        if "," in s and "." in s and s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
        return float(s)
    except Exception:
        return None

def compute_discount(now: Optional[float], before: Optional[float]) -> Optional[int]:
    if not now or not before or before <= 0:
        return None
    pct = int(round((1.0 - (now / before)) * 100))
    if pct < 0:
        return None
    return pct

def normalize_url(u: str) -> str:
    u = (u or "").strip().replace("\\/", "/")
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return BASE + u
    if u.startswith("http"):
        return u
    return BASE + "/" + u.lstrip("/")

# =========================
# Extract products from __NEXT_DATA__ (robusto)
# =========================
NEXT_DATA_RE = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)

def _walk(obj: Any, out: List[dict]) -> None:
    """Recorre JSON buscando objetos que parezcan productos."""
    if isinstance(obj, dict):
        # Heurística: objetos con 'productName'/'name' + link y precio
        keys = set(obj.keys())
        if ("productName" in keys or "name" in keys) and ("link" in keys or "linkText" in keys or "url" in keys):
            out.append(obj)
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, list):
        for it in obj:
            _walk(it, out)

def extract_products_from_next_data(html: str) -> List[Tuple[str, str, Optional[float], Optional[float], Optional[int]]]:
    """
    Devuelve lista de: (id_key, title, price_now, price_before, discount_pct, url)
    id_key es para dedupe.
    """
    m = NEXT_DATA_RE.search(html or "")
    if not m:
        return []

    raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except Exception:
        return []

    candidates: List[dict] = []
    _walk(data, candidates)

    products: List[Tuple[str, str, Optional[float], Optional[float], Optional[int], str]] = []
    seen: Set[str] = set()

    for c in candidates:
        title = (c.get("productName") or c.get("name") or "").strip()
        if not title:
            continue

        # URL
        link = c.get("url") or c.get("link") or c.get("linkText") or ""
        url = normalize_url(link)

        # Prices (varias rutas posibles)
        now = None
        before = None

        # VTEX style: items[0].sellers[0].commertialOffer
        try:
            items = c.get("items") or []
            if items:
                sellers = items[0].get("sellers") or []
                if sellers:
                    offer = sellers[0].get("commertialOffer") or {}
                    now = _to_float(offer.get("Price"))
                    before = _to_float(offer.get("ListPrice")) or _to_float(offer.get("PriceWithoutDiscount"))
        except Exception:
            pass

        # Some NextJS states store price fields directly
        if now is None:
            now = _to_float(c.get("price") or c.get("sellingPrice") or c.get("bestPrice"))

        if before is None:
            before = _to_float(c.get("listPrice") or c.get("originalPrice") or c.get("regularPrice"))

        disc = compute_discount(now, before)

        # Si ya trae descuento:
        if disc is None:
            disc = _to_float(c.get("discountPercentage"))
            if disc is not None:
                disc = int(round(disc))

        # ID para dedupe
        id_key = str(c.get("productId") or c.get("id") or url)

        if id_key in seen:
            continue
        seen.add(id_key)

        products.append((id_key, title, now, before, disc, url))

    return products

# =========================
# Fallback regex parsing
# =========================
# Busca urls tipo /p/ o /producto/ (PlazaVea varía)
FALLBACK_URL_RE = re.compile(r'href="(/[^"]*?/p/[^"]+|/[^"]*?/producto/[^"]+)"', re.IGNORECASE)
FALLBACK_PRICE_RE = re.compile(r"S/\s*([\d\.,]+)")

def extract_products_fallback(html: str) -> List[Tuple[str, str, Optional[float], Optional[float], Optional[int], str]]:
    urls = []
    seen = set()
    for m in FALLBACK_URL_RE.findall(html or ""):
        u = normalize_url(m)
        if u not in seen:
            seen.add(u)
            urls.append(u)

    # Sin título/antes, mandamos solo URL + precio “cercano”
    products = []
    for u in urls[:120]:
        products.append((u, "Producto PlazaVea", None, None, None, u))
    return products

# =========================
# Message
# =========================
def format_msg(title: str, disc: int, now: Optional[float], before: Optional[float], url: str) -> str:
    safe_title = html_lib.escape(title[:140])
    parts = [f"🟩 <b>{disc}% OFF</b>", f"🛒 <b>{safe_title}</b>"]
    if now is not None and before is not None:
        parts.append(f"💰 Ahora: <b>S/ {now:.2f}</b> | Antes: S/ {before:.2f}")
    elif now is not None:
        parts.append(f"💰 Precio: <b>S/ {now:.2f}</b>")
    parts.append(f"🔗 {html_lib.escape(url)}")
    return "\n".join(parts)

# =========================
# Main
# =========================
def main():
    category_urls = [u.strip() for u in ENV_CATEGORY_URLS.split(",") if u.strip()] if ENV_CATEGORY_URLS else DEFAULT_CATEGORY_URLS

    state = load_state()
    sent: Dict[str, Any] = state.get("sent", {})
    sent_set: Set[str] = set(sent.keys())

    s = _session()

    total_found = 0
    total_sent = 0
    blocked_pages = 0
    used_fallback = 0

    for cu in category_urls:
        for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
            page_url = cu if page == 1 else with_page_param(cu, page)
            log.info(f"Page: {page_url}")

            r = s.get(page_url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            html = r.text or ""

            if looks_blocked(html):
                blocked_pages += 1
                log.warning("Posible bloqueo detectado.")
                break

            products = extract_products_from_next_data(html)
            if not products:
                used_fallback += 1
                products = extract_products_fallback(html)

            # filtrar por descuento
            good = []
            for (id_key, title, now, before, disc, url) in products:
                if disc is None:
                    continue
                if disc < MIN_DISCOUNT_PERCENT:
                    continue
                good.append((id_key, title, now, before, disc, url))

            total_found += len(good)

            for (id_key, title, now, before, disc, url) in good:
                if id_key in sent_set:
                    continue
                telegram_send(format_msg(title, disc, now, before, url))
                total_sent += 1
                sent[id_key] = {"ts": int(time.time()), "discount": disc, "title": title, "url": url}
                sent_set.add(id_key)
                sleep_a_bit()

            sleep_a_bit()

    state["sent"] = sent
    state["last_run"] = int(time.time())
    save_state(state)

    resumen = (
        f"🟩 <b>Resumen PlazaVea</b>\n"
        f"🔥 Ofertas encontradas (>= {MIN_DISCOUNT_PERCENT}%): <b>{total_found}</b>\n"
        f"📨 Enviadas nuevas: <b>{total_sent}</b>\n"
        f"🧱 Páginas con posible bloqueo: <b>{blocked_pages}</b>\n"
        f"🧩 Fallback usado (sin JSON): <b>{used_fallback}</b>\n"
        f"🕒 {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    telegram_send(resumen)

if __name__ == "__main__":
    main()
