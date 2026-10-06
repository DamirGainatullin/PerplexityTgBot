import os
import sqlite3
import requests
import asyncio
import logging
import sys
from datetime import date
from urllib.parse import urlsplit, urlunsplit

from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import ReplyKeyboardRemove
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pathlib import Path
from sources_big import SOURCE_KEYS
from sources_big import collect_all_news
from sources_big import enrich_news_with_tavily_summary
from sources_big import first_check_filter_news
from sources_big import get_openrouter_request_kwargs
from sources_big import rewrite_summaries_with_model
from sources_big import ask_final_summary_model, build_final_summary_materials
from storage_maintenance import prune_bot_cache
from message_utils import split_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)


# ================== ENV ==================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
TELEGRAM_PROXY_URL = os.getenv("TELEGRAM_PROXY_URL", "").strip()

# ================== TELEGRAM ==================


def _mask_proxy_url(proxy_url: str) -> str:
    parts = urlsplit(proxy_url)
    if not parts.netloc or "@" not in parts.netloc:
        return proxy_url

    _, host_part = parts.netloc.rsplit("@", 1)
    return urlunsplit((parts.scheme, f"***@{host_part}", parts.path, parts.query, parts.fragment))


def build_telegram_bot() -> Bot:
    if TELEGRAM_PROXY_URL:
        logging.info("[TELEGRAM] proxy enabled: %s", _mask_proxy_url(TELEGRAM_PROXY_URL))
        return Bot(token=BOT_TOKEN, session=AiohttpSession(proxy=TELEGRAM_PROXY_URL))
    return Bot(token=BOT_TOKEN)


bot = build_telegram_bot()
dp = Dispatcher()

# ================== PROMPT ===================


def _read_text_with_fallback(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp1251")


def load_prompt():
    prompt_path = Path(__file__).parent / "prompt.txt"
    return _read_text_with_fallback(prompt_path)


# ================== DATABASE ==================
conn = sqlite3.connect(Path(__file__).with_name("bot.db"))
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS news_requests (
    chat_id INTEGER PRIMARY KEY,
    last_date TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS daily_news_cache (
    date TEXT PRIMARY KEY,
    content TEXT
)
""")

conn.commit()
prune_bot_cache(conn)

NO_NEWS = "NO_NEWS_LAST_24_HOURS"
TELEGRAM_MESSAGE_LIMIT = 4096


def truncate_text(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text

    cutoff = max_length - 3
    if cutoff <= 0:
        return text[:max_length]
    return text[:cutoff].rstrip() + "..."


def prepare_summary_text(summary: str) -> str:
    return summary.strip()


def ask_model(materials: str) -> str:
    return ask_final_summary_model(materials)


def _short_user_error(error: Exception) -> str:
    message = str(error).strip()
    if message:
        return message
    return "News are temporarily unavailable."


def build_news_message(news: str) -> str:
    return f"Сводка санкционных новостей:\n\n{news}"


# ================== BUSINESS LOGIC ==================
def _get_news_for_today_impl() -> str:
    prune_bot_cache(conn)
    today = date.today().isoformat()

    cursor.execute(
        "SELECT content FROM daily_news_cache WHERE date = ?",
        (today,)
    )
    row = cursor.fetchone()

    if row:
        logging.info("[CACHE HIT] date=%s chars=%s", today, len(row[0]))
        return row[0]

    news_items = collect_all_news()
    logging.info("[NEWS COLLECTED RAW] items=%s", len(news_items))

    if not news_items:
        text = "No sanctions news affecting Russia were found in the last 24 hours."

        cursor.execute(
            "INSERT INTO daily_news_cache (date, content) VALUES (?, ?)",
            (today, text)
        )
        conn.commit()

        return text

    filtered_items = first_check_filter_news(news_items)
    logging.info("[NEWS AFTER FIRST CHECK] items=%s", len(filtered_items))

    if not filtered_items:
        text = "No sanctions news affecting Russia were found in the last 24 hours."
        cursor.execute(
            "INSERT INTO daily_news_cache (date, content) VALUES (?, ?)",
            (today, text)
        )
        conn.commit()
        return text

    enriched_items = enrich_news_with_tavily_summary(filtered_items)
    logging.info("[NEWS AFTER ENRICH] items=%s", len(enriched_items))
    rewritten_items = rewrite_summaries_with_model(enriched_items)
    logging.info("[NEWS AFTER SUMMARY REWRITE] items=%s", len(rewritten_items))

    formatted = build_final_summary_materials(rewritten_items)

    logging.info("[NEWS INPUT] chars=%s", len(formatted))
    summary = ask_model(formatted)
    summary = prepare_summary_text(summary)
    logging.info("[NEWS SUMMARY] chars=%s date=%s", len(summary), today)

    cursor.execute(
        "INSERT INTO daily_news_cache (date, content) VALUES (?, ?)",
        (today, summary)
    )
    conn.commit()

    return summary


def get_news_for_today() -> str:
    try:
        return _get_news_for_today_impl()
    except requests.Timeout as e:
        logging.exception("[NEWS TIMEOUT] %s", e)
        raise RuntimeError("Source or API timed out.")
    except requests.RequestException as e:
        logging.exception("[NEWS REQUEST ERROR] %s", e)
        raise RuntimeError("Network error while collecting news.")
    except KeyError as e:
        logging.exception("[NEWS FORMAT ERROR] missing key: %s", e)
        raise RuntimeError("A source returned incomplete data.")
    except Exception as e:
        logging.exception("[NEWS ERROR] %s", e)
        raise RuntimeError("Failed to prepare the news summary.")


# ================== HANDLERS ==================
@dp.message(CommandStart())
async def start(message: types.Message):
    await message.answer(
        "Используй команду /news, чтобы получить сводку новостей.\n"
        "Доступно 1 раз в день для чата."
    )


@dp.message(Command("news"))
async def send_news(message: types.Message):
    chat_id = message.chat.id
    today = date.today().isoformat()

    cursor.execute(
        "INSERT OR IGNORE INTO chats (chat_id) VALUES (?)",
        (chat_id,)
    )
    conn.commit()

    cursor.execute(
        "SELECT last_date FROM news_requests WHERE chat_id = ?",
        (chat_id,)
    )
    row = cursor.fetchone()

    if row and row[0] == today:
        await message.answer(
            "Сегодня новости уже публиковались.\n"
            "Теперь они будут приходить автоматически в 9:00."
        )
        return

    await message.answer("Собираю новости...")

    try:
        news = get_news_for_today()
        message_text = build_news_message(news)

        for chunk in split_message(message_text):
            await message.answer(chunk)
        cursor.execute(
            "INSERT OR REPLACE INTO news_requests (chat_id, last_date) VALUES (?, ?)",
            (chat_id, today)
        )
        conn.commit()
        return

        await message.answer(f"Сводка санкционных новостей:\n\n{news}")

    except Exception as e:
        logging.exception("Failed to send news to chat_id=%s", chat_id)
        await message.answer(_short_user_error(e))
        return
        await message.answer("Не удалось получить новости")


async def send_daily_news():
    today = date.today().isoformat()

    cursor.execute("SELECT chat_id FROM chats")
    chats = cursor.fetchall()

    try:
        news = get_news_for_today()
        message_text = build_news_message(news)
    except Exception as e:
        logging.exception("Ошибка получения новостей")
        return

    for (chat_id,) in chats:
        cursor.execute(
            "SELECT last_date FROM news_requests WHERE chat_id = ?",
            (chat_id,)
        )
        row = cursor.fetchone()

        if row and row[0] == today:
            continue

        try:
            for chunk in split_message(message_text):
                await bot.send_message(chat_id, chunk, reply_markup=ReplyKeyboardRemove())
            cursor.execute(
                "INSERT OR REPLACE INTO news_requests (chat_id, last_date) VALUES (?, ?)",
                (chat_id, today)
            )
            conn.commit()
        except Exception:
            logging.exception("Failed to send daily news to chat_id=%s", chat_id)
        continue

        await bot.send_message(
            chat_id,
            f"Сводка санкционных новостей:\n\n{news}"
        )


# ================== START ==================
async def main():
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    scheduler.add_job(
        send_daily_news,
        trigger="cron",
        hour=8,
        minute=45
    )

    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
