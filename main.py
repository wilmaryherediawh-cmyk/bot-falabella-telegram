import os, re, json, time, random, logging, html as html_lib
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests

BASE = "https://www.falabella.com.pe"

DEFAULT_CATEGORY_URLS = [
    "https://www.falabella.com.pe/falabella-pe/category/CATG12023/Mujer?f.derived.variant.sellerId=FALABELLA",
    "https://www.falabella.com.pe/falabella-pe/category/cat40498/Belleza--higiene-y-salud?f.derived.variant.sellerId=FALABELLA",
    "https://www.falabella.com.pe/falabella-pe/category/CATG33544/Ninos-y-Jugueteria?f.derived.variant.sellerId=FALABELLA",
]

ENV_CATEGORY_URLS = os.getenv("CATEGORY_URLS", "").strip()

MIN_DISCOUNT_PERCENT = int(os.getenv("MIN_DISCOUNT_PERCENT", "50"))
MAX_PAGES_PER_CATEGORY = int(os.getenv("MAX_PAGES_PER_CATEGORY", "6"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
STATE_FILE = "state.json"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("falabella_bot")

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
        "Accept-Language": "es-PE,es;q=0.9,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": BASE + "/falabella-pe",
    })
    return s

def sleep_a_bit():
    time.sleep(random.uniform(0.8, 1.6))

def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"sent": {}, "last_run": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sent": {}, "last_run": None}

def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def telegram_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Faltan BOT_TOKEN o CHAT_ID en Secrets/Variables.")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False, "parse_mode": "HTML"}
    r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")

def with_page_param(url: str, page: int) -> str:
    p = urlparse(url)
    q = parse_qs(p.query)
    q["page"] = [str(page)]
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q, doseq=True), p.fragment))

def looks_blocked(html: str) -> bool:
    h = html.lower()
    return any(x in h for x in ["captcha", "verify you are human", "access denied", "unusual traffic", "robot"])

def normalize_url(u: str) -> str:
    u = u.strip().replace("\\/", "/")
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return BASE + u
    return u

# --------- PARSEO DESDE LISTADO ----------
# Sacamos "bloques" alrededor de cada /product/ y de ahí intentamos descuento + precio.
PRODUCT_BLOCK_RE = re.compile(r"(.{0,800})\/falabella-pe\/product\/([^\"\'\s<>]+)(.{0,1200})", re.DOTALL)
DISCOUNT_RE = re.compile(r"-\s*(\d{1,3})\s*%")
PRICE_RE = re.compile(r"S\/\s*([\d\.,]+)")

def to_float_price(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip()
    # normaliza 1.234,56 -> 1234.56
    if "," in s and "." in s and s.rfind(",") > s.rfind("."):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except Exception:
        return None

def extract_offers_from_listing(html: str) -> List[Tuple[str, Optional[int], Optional[float]]]:
    offers = []
    seen = set()

    for m in PRODUCT_BLOCK_RE.finditer(html):
        around = (m.group(1) + m.group(0) + m.group(3))
        path = "/falabella-pe/product/" + m.group(2)
        url = normalize_url(path)

        if url in seen:
            continue
        seen.add(url)

        disc = None
        dm = DISCOUNT_RE.search(around)
        if dm:
            try:
                disc = int(dm.group(1))
            except Exception:
                disc = None

        # toma el primer precio que encuentre cerca
        price = None
        pm = PRICE_RE.findall(around)
        if pm:
            price = to_float_price(pm[0])

        offers.append((url, disc, price))

    return offers

def format_msg(url: str, disc: int, price: Optional[float]) -> str:
    parts = [f"🍄 <b>{disc}% OFF</b>"]
    if price is not None:
        parts.append(f"💰 <b>S/ {price:.2f}</b>")
    parts.append(f"🔗 {html_lib.escape(url)}")
    return "\n".join(parts)

def main():
    category_urls = [u.strip() for u in ENV_CATEGORY_URLS.split(",") if u.strip()] if ENV_CATEGORY_URLS else DEFAULT_CATEGORY_URLS
    state = load_state()
    sent = state.get("sent", {})
    sent_set: Set[str] = set(sent.keys())

    s = _session()

    total_offers_found = 0
    total_sent = 0
    blocked_pages = 0
    html_short_pages = 0

    for cu in category_urls:
        for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
            page_url = cu if page == 1 else with_page_param(cu, page)
            log.info(f"Cat page: {page_url}")

            r = s.get(page_url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            html = r.text or ""

            if len(html) < 50000:
                html_short_pages += 1

            if looks_blocked(html):
                blocked_pages += 1
                log.warning("Posible bloqueo/captcha en listado.")
                break

            offers = extract_offers_from_listing(html)

            # Filtra por descuento
            good = [(u, d, p) for (u, d, p) in offers if isinstance(d, int) and d >= MIN_DISCOUNT_PERCENT]
            total_offers_found += len(good)

            for (u, d, p) in good:
                if u in sent_set:
                    continue
                telegram_send(format_msg(u, d, p))
                total_sent += 1
                sent[u] = {"ts": int(time.time()), "discount": d}
                sent_set.add(u)
                sleep_a_bit()

            sleep_a_bit()

    state["sent"] = sent
    state["last_run"] = int(time.time())
    save_state(state)

    resumen = (
        f"🍄 <b>Resumen</b>\n"
        f"🔥 Ofertas encontradas (>= {MIN_DISCOUNT_PERCENT}%): <b>{total_offers_found}</b>\n"
        f"📨 Enviadas nuevas: <b>{total_sent}</b>\n"
        f"🧱 Páginas con posible bloqueo: <b>{blocked_pages}</b>\n"
        f"📄 Páginas con HTML corto: <b>{html_short_pages}</b>\n"
        f"🕒 {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    telegram_send(resumen)

if __name__ == "__main__":
    main()
