import datetime as dt
import logging
import os
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]  # your Telegram token, the only key needed
DB_FILE = os.environ.get("DB_PATH", "users.db")
DEFAULT_TZ = "Africa/Lagos"
DEFAULT_TIME = "08:00"


# ---------- storage ----------
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "chat_id INTEGER PRIMARY KEY, time TEXT, tz TEXT)"
    )
    return conn


def get_user(chat_id):
    with db() as c:
        return c.execute(
            "SELECT time, tz FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()


def save_user(chat_id, time, tz):
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO users VALUES (?, ?, ?)", (chat_id, time, tz)
        )


def delete_user(chat_id):
    with db() as c:
        c.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))


def all_users():
    with db() as c:
        return c.execute("SELECT chat_id, time, tz FROM users").fetchall()


# ---------- market data ----------
def arrow(pct):
    return "🟢" if pct >= 0 else "🔴"


def money(x):
    if x >= 1:
        return f"${x:,.2f}"
    return f"${x:.6f}".rstrip("0")


async def build_briefing():
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 100,
                "page": 1,
                "price_change_percentage": "24h",
            },
        )
        r.raise_for_status()
        coins = r.json()

        fear = None
        try:
            f = await client.get("https://api.alternative.me/fng/")
            d = f.json()["data"][0]
            fear = (d["value"], d["value_classification"])
        except Exception:
            pass

        btc_ngn = None
        try:
            n = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "ngn"},
            )
            btc_ngn = n.json()["bitcoin"]["ngn"]
        except Exception:
            pass

    today = dt.datetime.now(dt.timezone.utc).strftime("%A, %d %B %Y")
    lines = [f"☀️ Daily Crypto Briefing\n{today}\n", "📊 Top coins"]

    for c in coins[:5]:
        pct = c.get("price_change_percentage_24h") or 0
        lines.append(
            f"{arrow(pct)} {c['symbol'].upper()}  {money(c['current_price'])}  ({pct:+.2f}%)"
        )

    movers = [c for c in coins if c.get("price_change_percentage_24h") is not None]
    movers.sort(key=lambda c: c["price_change_percentage_24h"], reverse=True)

    lines.append("\n🚀 Top gainers (top 100)")
    for c in movers[:3]:
        lines.append(
            f"{c['symbol'].upper()}  {c['price_change_percentage_24h']:+.2f}%"
        )

    lines.append("\n📉 Top losers (top 100)")
    for c in movers[-3:][::-1]:
        lines.append(
            f"{c['symbol'].upper()}  {c['price_change_percentage_24h']:+.2f}%"
        )

    if fear:
        lines.append(f"\n😨 Fear & Greed Index: {fear[0]} ({fear[1]})")
    if btc_ngn:
        lines.append(f"🇳🇬 1 BTC ≈ ₦{btc_ngn:,.0f}")

    lines.append("\nNot financial advice.")
    return "\n".join(lines)


# ---------- scheduling ----------
async def send_daily(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    try:
        text = await build_briefing()
    except Exception:
        text = "Sorry, I couldn't fetch the market data this morning. Try /briefing in a bit."
    await context.bot.send_message(chat_id, text)


def schedule(app: Application, chat_id: int, hhmm: str, tz: str):
    for job in app.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    h, m = map(int, hhmm.split(":"))
    app.job_queue.run_daily(
        send_daily,
        time=dt.time(h, m, tzinfo=ZoneInfo(tz)),
        chat_id=chat_id,
        name=str(chat_id),
    )


async def restore_jobs(app: Application):
    for chat_id, hhmm, tz in all_users():
        schedule(app, chat_id, hhmm, tz)


# ---------- commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome! I send a crypto market briefing every morning.\n\n"
        "/briefing - get today's briefing now\n"
        f"/subscribe 08:00 - get it daily at that time (default {DEFAULT_TIME})\n"
        f"/timezone Africa/Lagos - set your timezone (default {DEFAULT_TZ})\n"
        "/unsubscribe - stop the daily message"
    )


async def briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching the latest data...")
    try:
        await update.message.reply_text(await build_briefing())
    except Exception:
        await update.message.reply_text(
            "Couldn't reach the market data right now. Please try again shortly."
        )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    existing = get_user(chat_id)
    tz = existing[1] if existing else DEFAULT_TZ
    hhmm = DEFAULT_TIME
    if context.args:
        try:
            h, m = map(int, context.args[0].split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
            hhmm = f"{h:02d}:{m:02d}"
        except Exception:
            await update.message.reply_text("Use the format /subscribe 08:00")
            return
    save_user(chat_id, hhmm, tz)
    schedule(context.application, chat_id, hhmm, tz)
    await update.message.reply_text(
        f"Done! You'll get your briefing every day at {hhmm} ({tz})."
    )


async def timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Use the format /timezone Africa/Lagos")
        return
    tz = context.args[0]
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        await update.message.reply_text(
            "I don't know that timezone. Try something like Africa/Lagos or Europe/London."
        )
        return
    existing = get_user(chat_id)
    hhmm = existing[0] if existing else DEFAULT_TIME
    save_user(chat_id, hhmm, tz)
    schedule(context.application, chat_id, hhmm, tz)
    await update.message.reply_text(f"Timezone set to {tz}. Daily time: {hhmm}.")


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    delete_user(chat_id)
    for job in context.application.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    await update.message.reply_text("Unsubscribed. Send /subscribe anytime to come back.")


def main():
    app = Application.builder().token(TOKEN).post_init(restore_jobs).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("briefing", briefing))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("timezone", timezone))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.run_polling()


if __name__ == "__main__":
    main()
