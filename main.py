import os
import re
import json
import hashlib
import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]

WATCH_URLS = [
    "https://www.falabella.com.pe/falabella-pe/collection/ofertas",
]

THRESHOLD = 50  # >= 50%
CHECK_EVERY_SECONDS = 30 * 60  # 30 min
STATE_FILE = "state.json"

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"sent": [], "chat_id": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def stable_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def scrape_offers(url: str):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "lxml")

    results = []

    # Busca "-XX%" en el HTML
    for node in soup.find_all(string=re.compile(r"-\s*\d{1,2}%")):
        m = re.search(r"-\s*(\d{1,2})%", str(node))
        if not m:
            continue

        pct = int(m.group(1))
        if pct < THRESHOLD:
            continue

        # intenta encontrar link cercano
        parent = node.parent
        link = None
        name = None

        for _ in range(7):
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
            parent = parent.parent

        if not link:
            continue

        if not name:
            name = "Producto en oferta"

        results.append({"name": name[:120], "url": link, "pct": pct})

    # quitar duplicados por URL
    uniq = {}
    for it in results:
        uniq[it["url"]] = it

    return list(uniq.values())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    state["chat_id"] = update.effective_chat.id
    save_state(state)

    await update.message.reply_text(
        "✅ Listo.\n"
        "Te avisaré cuando encuentre productos con 50% o más de descuento.\n\n"
        "Comandos:\n"
        "/check = revisar ahora\n"
        "/urls = ver URLs\n"
    )


async def urls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "🔎 URLs monitoreadas:\n" + "\n".join(f"- {u}" for u in WATCH_URLS)
    await update.message.reply_text(msg)


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Revisando…")
    await run_check_and_alert(update.effective_chat.id, context.application)
    await update.message.reply_text("✅ Listo.")


async def run_check_and_alert(chat_id: int, app: Application):
    state = load_state()
    sent = set(state.get("sent", []))

    hits = []

    for url in WATCH_URLS:
        try:
            items = scrape_offers(url)
            for it in items:
                item_id = stable_id(it["url"])
                if item_id not in sent:
                    hits.append(it | {"id": item_id})
        except Exception:
            continue

    if not hits:
        return

    # manda máximo 10 por ronda
    for it in hits[:10]:
        text = (
            f"🔥 OFERTA DETECTADA\n"
            f"{it['pct']}% OFF\n"
            f"{it['name']}\n"
            f"{it['url']}"
        )
        await app.bot.send_message(chat_id=chat_id, text=text)
        sent.add(it["id"])

    state["sent"] = list(sent)[-2000:]
    save_state(state)


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await run_check_and_alert(chat_id, context.application)


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("urls", urls))

    state = load_state()
    chat_id = state.get("chat_id")

    if chat_id:
        app.job_queue.run_repeating(
            scheduled_job,
            interval=CHECK_EVERY_SECONDS,
            first=10,
            data={"chat_id": chat_id},
            name="offer_scanner",
        )

    app.run_polling()


if __name__ == "__main__":
    main()
