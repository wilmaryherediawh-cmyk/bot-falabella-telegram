import os
import re
import json
import hashlib
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]  # número (puede ser negativo si es grupo)

WATCH_URLS = [
    "https://www.falabella.com.pe/falabella-pe/collection/ofertas",
]

THRESHOLD = 50  # >= 50%
STATE_FILE = "state.json"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"sent": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def stable_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def telegram_send(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=30)
    r.raise_for_status()


def scrape_discounts(url: str):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    items = []
    for node in soup.find_all(string=re.compile(r"-\s*\d{1,2}%")):
        m = re.search(r"-\s*(\d{1,2})%", str(node))
        if not m:
            continue
        pct = int(m.group(1))
        if pct < THRESHOLD:
            continue

        parent = node.parent
        link = None
        name = None

        for _ in range(8):
            if parent is None:
                break
            if hasattr(parent, "find"):
                a = parent.find("a", href=True)
                if a and a.get("href"):
                    link = a["href"]
                    if link.startswith("/"):
                        link = "https://www.falabella.com.pe" + link
                    name = a.get_text(" ", strip=True)
                    break
            parent = getattr(parent, "parent", None)

        if link:
            if not name:
                name = "Producto en oferta"
            items.append({"pct": pct, "name": name[:120], "url": link})

    # dedupe por url
    uniq = {}
    for it in items:
        uniq[it["url"]] = it
    return list(uniq.values())


def main():
    state = load_state()
    sent = set(state.get("sent", []))

    new_hits = []
    for url in WATCH_URLS:
        try:
            for it in scrape_discounts(url):
                it_id = stable_id(it["url"])
                if it_id not in sent:
                    it["id"] = it_id
                    new_hits.append(it)
        except Exception:
            continue

    if not new_hits:
        telegram_send("✅ Revisé Falabella y no encontré ofertas nuevas ≥ 50% (o la página no mostró % hoy).")
        return

    # manda máximo 10
    for it in new_hits[:10]:
        telegram_send(f"🔥 {it['pct']}% OFF\n{it['name']}\n{it['url']}")
        sent.add(it["id"])

    state["sent"] = list(sent)[-3000:]
    save_state(state)


if __name__ == "__main__":
    main()
