import os
import re
import json
import time
import random
import logging
import html as html_lib
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus, urljoin

import requests

# ============================================================
# Config (ajustable)
# ============================================================

BASE = "https://www.falabella.com.pe"
SEARCH_URL = BASE + "/falabella-pe/search?Ntt={query}"

DEFAULT_KEYWORDS = [
    "niño",
    "bebe",
    "moda",
    "juguete",
    "belleza",
    "calzado",
    "accesorios mujer",
]

MIN_DISCOUNT_PERCENT = int(os.getenv("MIN_DISCOUNT_PERCENT", "50"))
MAX_PRODUCTS_PER_KEYWORD = int(os.getenv("MAX_PRODUCTS_PER_KEYWORD", "30"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
STATE_FILE = "state.json"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

ENV_KEYWORDS = os.getenv("KEYWORDS", "").strip()

# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("falabella_bot")


# ============================================================
# Helpers
# ============================================================

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
        ),
        "Accept-Language": "es-PE,es;q=0.9,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    return s


def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"sent": {}, "last_run": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"No pude leer {STATE_FILE}: {e}. Creo estado nuevo.")
        return {"sent": {}, "last_run": None}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def normalize_url(u: str) -> str:
    u = u.strip()
    # des-escapar cosas tipo \/falabella-pe\/product\/...
    u = u.replace("\\/", "/")

    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return urljoin(BASE, u)
    return u


def sleep_a_bit() -> None:
    time.sleep(random.uniform(0.6, 1.4))


# ============================================================
# Telegram
# ============================================================

def telegram_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Faltan BOT_TOKEN o CHAT_ID en Secrets (Settings → Secrets and variables → Actions).")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
        "parse_mode": "HTML",
    }

    r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")


# ============================================================
# Scraping
# ============================================================

@dataclass
class Product:
    url: str
    title: str
    price_now: Optional[float]
    price_before: Optional[float]
    discount_pct: Optional[int]


_price_number_re = re.compile(r"(\d[\d\.,]+)")

def _to_float_price(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip()
    m = _price_number_re.search(s)
    if not m:
        return None
    num = m.group(1)

    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    else:
        if "," in num and num.count(",") == 1 and len(num.split(",")[-1]) in (1, 2):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "").replace("..", ".")

    try:
        return float(num)
    except Exception:
        return None


def extract_product_links_from_search(html: str) -> List[str]:
    """
    Falabella a veces renderiza distinto:
    - href="/falabella-pe/product/..."
    - href='...'
    - url:"\/falabella-pe\/product\/..."
    - URL completa https://www.falabella.com.pe/falabella-pe/product/...
    """
    patterns = [
        r'href="(/falabella-pe/product/[^"]+)"',
        r"href='(/falabella-pe/product/[^']+)'",
        r'href="(https?://www\.falabella\.com\.pe/falabella-pe/product/[^"]+)"',
        r"href='(https?://www\.falabella\.com\.pe/falabella-pe/product/[^']+)'",
        r'"url"\s*:\s*"((?:\\\/|\/)falabella-pe(?:\\\/|\/)product(?:\\\/|\/)[^"]+)"',
        r'"linkTo"\s*:\s*"((?:\\\/|\/)falabella-pe(?:\\\/|\/)product(?:\\\/|\/)[^"]+)"',
        r'(https?://www\.falabella\.com\.pe/falabella-pe/product/\d+/[^\s"\'<>]+)',
    ]

    links: Set[str] = set()
    for pat in patterns:
        for m in re.findall(pat, html):
            links.add(normalize_url(m))

    # filtra cosas raras (como /falabella-pe/product/ algo incompleto)
    clean = []
    for l in links:
        if "/falabella-pe/product/" in l:
            clean.append(l)

    # dedupe preservando orden
    seen = set()
    ordered = []
    for l in clean:
        if l not in seen:
            seen.add(l)
            ordered.append(l)

    return ordered


def extract_title(html: str) -> str:
    m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
    if m:
        return m.group(1).strip()
    m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return "Producto"


def extract_discount_from_html(html: str) -> Optional[int]:
    m = re.search(r"-\s*(\d{1,2})\s*%", html)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None

    m = re.search(r"(\d{1,2})\s*%\s*OFF", html, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None

    return None


def extract_prices(html: str) -> Tuple[Optional[float], Optional[float]]:
    candidates_now: List[float] = []
    candidates_before: List[float] = []

    # JSON-like keys (cuando existan)
    for key in ["price", "bestPrice", "internetPrice", "salePrice", "currentPrice"]:
        for m in re.findall(rf'"{key}"\s*:\s*"?(.*?)"?(,|\}}|\])', html):
            val = _to_float_price(m[0])
            if val:
                candidates_now.append(val)

    for key in ["originalPrice", "listPrice", "normalPrice", "oldPrice", "strikePrice"]:
        for m in re.findall(rf'"{key}"\s*:\s*"?(.*?)"?(,|\}}|\])', html):
            val = _to_float_price(m[0])
            if val:
                candidates_before.append(val)

    # meta itemprop price
    m = re.search(r'itemprop="price"\s+content="([^"]+)"', html)
    if m:
        val = _to_float_price(m.group(1))
        if val:
            candidates_now.append(val)

    # fallback: "S/ 11.97" (como tu ejemplo)
    soles = []
    for m in re.findall(r"S/\s*([\d\.,]+)", html):
        val = _to_float_price(m)
        if val:
            soles.append(val)

    if not candidates_now and soles:
        uniq = sorted(set(soles))
        candidates_now.append(uniq[0])
        if len(uniq) >= 2:
            candidates_before.append(uniq[-1])

    now = min(candidates_now) if candidates_now else None
    before = max(candidates_before) if candidates_before else None

    if now and before and before <= now:
        before = None

    return now, before


def compute_discount(now: Optional[float], before: Optional[float]) -> Optional[int]:
    if not now or not before or before <= 0:
        return None
    pct = int(round((1.0 - (now / before)) * 100))
    if pct < 0:
        return None
    return pct


def fetch_product(session: requests.Session, url: str) -> Product:
    r = session.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    html = r.text

    title = extract_title(html)
    now, before = extract_prices(html)

    disc = compute_discount(now, before)
    if disc is None:
        disc = extract_discount_from_html(html)

    return Product(url=url, title=title, price_now=now, price_before=before, discount_pct=disc)


def fetch_candidates(session: requests.Session, keyword: str) -> List[str]:
    url = SEARCH_URL.format(query=quote_plus(keyword))
    log.info(f"Buscando keyword: {keyword} -> {url}")

    # a veces ayuda mandar un referer
    headers = {"Referer": BASE + "/falabella-pe"}
    r = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

    links = extract_product_links_from_search(r.text)
    log.info(f"Links encontrados para '{keyword}': {len(links)}")

    return links[:MAX_PRODUCTS_PER_KEYWORD]


def format_msg(p: Product) -> str:
    safe_title = html_lib.escape(p.title)

    parts = []
    if p.discount_pct is not None:
        parts.append(f"⚡ <b>{p.discount_pct}% OFF</b>")
    else:
        parts.append("⚡ <b>OFERTA</b>")

    parts.append(f"🛍️ <b>{safe_title}</b>")

    if p.price_now and p.price_before:
        parts.append(f"💰 Ahora: <b>S/ {p.price_now:,.2f}</b>  |  Antes: S/ {p.price_before:,.2f}")
    elif p.price_now:
        parts.append(f"💰 Precio: <b>S/ {p.price_now:,.2f}</b>")

    parts.append(f"🔗 {p.url}")
    return "\n".join(parts)


# ============================================================
# Main
# ============================================================

def main():
    if ENV_KEYWORDS:
        keywords = [k.strip() for k in ENV_KEYWORDS.split(",") if k.strip()]
    else:
        keywords = DEFAULT_KEYWORDS

    state = load_state()
    sent: Dict[str, Dict] = state.get("sent", {})
    sent_set: Set[str] = set(sent.keys())

    s = _session()

    candidate_urls: List[str] = []
    per_kw_counts = {}

    for kw in keywords:
        try:
            urls = fetch_candidates(s, kw)
            per_kw_counts[kw] = len(urls)
            candidate_urls.extend(urls)
            sleep_a_bit()
        except Exception as e:
            per_kw_counts[kw] = 0
            log.warning(f"Falló búsqueda para '{kw}': {e}")

    # Debug útil: si no hay productos, te avisamos por Telegram
    if not candidate_urls:
        msg = "⚠️ El bot corrió, pero Falabella devolvió 0 productos en SEARCH.\n\n"
        msg += "Conteo por keyword:\n"
        for kw, c in per_kw_counts.items():
            msg += f"• {kw}: {c}\n"
        telegram_send(msg)
        return

    # Dedupe global
    deduped = []
    seen = set()
    for u in candidate_urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)

    log.info(f"Total URLs candidatas (dedupe): {len(deduped)}")

    total_checked = 0
    total_found = 0
    total_sent = 0

    for url in deduped:
        total_checked += 1
        try:
            p = fetch_product(s, url)
            sleep_a_bit()

            if p.discount_pct is None or p.discount_pct < MIN_DISCOUNT_PERCENT:
                continue

            total_found += 1

            if p.url in sent_set:
                continue

            telegram_send(format_msg(p))
            total_sent += 1

            sent[p.url] = {"ts": int(time.time()), "discount": p.discount_pct, "title": p.title}
            sent_set.add(p.url)

            log.info(f"Enviado: {p.discount_pct}% | {p.title}")

        except Exception as e:
            log.warning(f"Error en producto {url}: {e}")

    state["sent"] = sent
    state["last_run"] = int(time.time())
    save_state(state)

    log.info("======================================")
    log.info(f"Revisados: {total_checked}")
    log.info(f"Con descuento >= {MIN_DISCOUNT_PERCENT}%: {total_found}")
    log.info(f"Enviados a Telegram: {total_sent}")
    log.info("======================================")


if __name__ == "__main__":
    main()
