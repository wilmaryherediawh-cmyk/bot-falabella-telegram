import os
import re
import json
import time
import math
import random
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus, urljoin

import requests

# ============================================================
# Config (puedes ajustar)
# ============================================================

BASE = "https://www.falabella.com.pe"
SEARCH_URL = BASE + "/falabella-pe/search?Ntt={query}"

# Palabras clave / categorías que quieres rastrear
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
MAX_PRODUCTS_PER_KEYWORD = int(os.getenv("MAX_PRODUCTS_PER_KEYWORD", "30"))  # para no hacer demasiadas requests
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
STATE_FILE = "state.json"

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Opcional: sobreescribir keywords desde env (separadas por coma)
# ejemplo: KEYWORDS="niño,bebe,moda"
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
    })
    return s


def load_state() -> Dict:
    """state.json guarda links ya enviados para evitar repetidos."""
    if not os.path.exists(STATE_FILE):
        return {"sent": {}, "last_run": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"No pude leer {STATE_FILE}: {e}. Creo estado nuevo.")
        return {"sent": {}, "last_run": None}


def save_state(state: Dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"No pude guardar {STATE_FILE}: {e}")
        raise


def normalize_url(u: str) -> str:
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
# Scraping (busca productos por keyword y luego visita el producto)
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
    """
    Convierte "1,299.90" o "1.299,90" o "1299" a float.
    """
    if not s:
        return None
    s = s.strip()
    m = _price_number_re.search(s)
    if not m:
        return None
    num = m.group(1)

    # normaliza: si hay ambos separadores, asume el último como decimal
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            # 1.299,90
            num = num.replace(".", "").replace(",", ".")
        else:
            # 1,299.90
            num = num.replace(",", "")
    else:
        # solo coma: puede ser decimal
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
    En Falabella, los links suelen verse como:
    /falabella-pe/product/xxxxx/Nombre
    """
    links = set(re.findall(r'href="(/falabella-pe/product/[^"]+)"', html))
    # A veces hay links con \u002F en JS; intentamos también
    links |= set(re.findall(r'\"url\"\s*:\s*\"(\/falabella-pe\/product\/[^\"]+)\"', html))
    out = [normalize_url(l) for l in links]
    return out


def extract_title(html: str) -> str:
    # og:title suele funcionar
    m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
    if m:
        return m.group(1).strip()
    # fallback: <title>
    m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return "Producto"


def extract_prices(html: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Intenta encontrar precio actual y precio anterior.
    No hay un solo formato estable, así que probamos varias estrategias.
    """
    # Estrategia 1: JSON-like keys comunes
    candidates_now = []
    candidates_before = []

    # claves que a veces aparecen
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

    # Estrategia 2: meta itemprop price
    m = re.search(r'itemprop="price"\s+content="([^"]+)"', html)
    if m:
        val = _to_float_price(m.group(1))
        if val:
            candidates_now.append(val)

    # Limpia y elige: precio actual = el menor valor razonable; antes = el mayor
    # (porque lo normal es: antes > ahora)
    now = min(candidates_now) if candidates_now else None
    before = max(candidates_before) if candidates_before else None

    # Si before no existe pero hay varios now, a veces el mayor es el "antes"
    if before is None and len(candidates_now) >= 2:
        sorted_vals = sorted(set(candidates_now))
        before = sorted_vals[-1]
        now = sorted_vals[0]

    # Validación básica
    if now and before and before <= now:
        # no tiene sentido, dejamos before como None
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

    return Product(
        url=url,
        title=title,
        price_now=now,
        price_before=before,
        discount_pct=disc,
    )


def fetch_candidates(session: requests.Session, keyword: str) -> List[str]:
    url = SEARCH_URL.format(query=quote_plus(keyword))
    log.info(f"Buscando keyword: {keyword} -> {url}")
    r = session.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    links = extract_product_links_from_search(r.text)

    # dedupe preservando orden
    seen = set()
    ordered = []
    for l in links:
        if l not in seen:
            seen.add(l)
            ordered.append(l)

    return ordered[:MAX_PRODUCTS_PER_KEYWORD]


def format_msg(p: Product) -> str:
    # Mensaje estilo ofertas
    parts = []
    if p.discount_pct is not None:
        parts.append(f"🔥 <b>{p.discount_pct}% OFF</b>")
    else:
        parts.append("🔥 <b>OFERTA</b>")

    parts.append(f"<b>{p.title}</b>")

    if p.price_now and p.price_before:
        parts.append(f"💸 Ahora: <b>S/ {p.price_now:,.2f}</b>  |  Antes: S/ {p.price_before:,.2f}")
    elif p.price_now:
        parts.append(f"💸 Precio: <b>S/ {p.price_now:,.2f}</b>")

    parts.append(p.url)
    return "\n".join(parts)


# ============================================================
# Main
# ============================================================

def main():
    # Keywords
    if ENV_KEYWORDS:
        keywords = [k.strip() for k in ENV_KEYWORDS.split(",") if k.strip()]
    else:
        keywords = DEFAULT_KEYWORDS

    state = load_state()
    sent: Dict[str, Dict] = state.get("sent", {})  # url -> {ts, discount}
    sent_set: Set[str] = set(sent.keys())

    s = _session()

    total_checked = 0
    total_found = 0
    total_sent = 0

    # Recolecta URLs candidatas
    candidate_urls: List[str] = []
    for kw in keywords:
        try:
            urls = fetch_candidates(s, kw)
            candidate_urls.extend(urls)
            sleep_a_bit()
        except Exception as e:
            log.warning(f"Falló búsqueda para '{kw}': {e}")

    # Dedupe global
    deduped = []
    seen = set()
    for u in candidate_urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)

    log.info(f"Total URLs candidatas (dedupe): {len(deduped)}")

    # Visita productos, calcula descuento y manda a Telegram si aplica
    for url in deduped:
        total_checked += 1

        try:
            p = fetch_product(s, url)
            sleep_a_bit()

            if p.discount_pct is None:
                continue

            if p.discount_pct < MIN_DISCOUNT_PERCENT:
                continue

            total_found += 1

            # Anti repetidos
            if p.url in sent_set:
                continue

            # Envía
            msg = format_msg(p)
            telegram_send(msg)
            total_sent += 1

            sent[p.url] = {"ts": int(time.time()), "discount": p.discount_pct, "title": p.title}
            sent_set.add(p.url)

            log.info(f"Enviado: {p.discount_pct}% | {p.title}")

        except Exception as e:
            log.warning(f"Error en producto {url}: {e}")

    # Actualiza estado
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
