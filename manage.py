import os
import sqlite3
import requests
import asyncio
from datetime import date

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pathlib import Path
from sources_big import SOURCE_KEYS
from sources_big import collect_all_news


# ================== ENV ==================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ================== TELEGRAM ==================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Новости")]],
    resize_keyboard=True
)
# ================== PROMPT ===================


def load_prompt():
    prompt_path = Path(__file__).parent / "prompt.txt"
    return prompt_path.read_text(encoding="utf-8")


# ================== DATABASE ==================
conn = sqlite3.connect("bot.db")
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

NO_NEWS = "NO_NEWS_LAST_24_HOURS"
TELEGRAM_MESSAGE_LIMIT = 4096
SAFE_SUMMARY_LIMIT = 3500


def truncate_text(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text

    cutoff = max_length - 3
    if cutoff <= 0:
        return text[:max_length]
    return text[:cutoff].rstrip() + "..."


def prepare_summary_text(summary: str) -> str:
    prepared = summary.strip()
    if len(prepared) > SAFE_SUMMARY_LIMIT:
        print(
            "[OPENAI WARN]",
            f"summary_too_long={len(prepared)}",
            f"truncating_to={SAFE_SUMMARY_LIMIT}"
        )
        prepared = truncate_text(prepared, SAFE_SUMMARY_LIMIT)

    if len(prepared) > SAFE_SUMMARY_LIMIT:
        raise RuntimeError("Prepared summary is too long for Telegram.")

    return prepared


# ================== OPENAI ==================
def _legacy_ask_model(materials: str) -> str:
    url = "https://api.perplexity.ai/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    PROMPT = load_prompt()

    payload = {
        "model": "gpt-4.1-mini",
        "temperature": 0.1,
        "messages": [
            {
                "role": "system",
                "content": PROMPT
            },
            {
                "role": "user",
                "content": materials
            }
        ]
    }

    response = requests.post(url, json=payload, headers=headers, timeout=60)
    response.raise_for_status()

    result = response.json()["choices"][0]["message"]["content"].strip()
    # print(result)
    if result == "NO_NEWS_LAST_24_HOURS":
        return "За последние 24 часа санкционных новостей не опубликовано."

    # return result
    sources_str = "Проверенные источники: " + ", ".join(SOURCE_KEYS)
    return f"{result}\n\n{sources_str}"


def ask_model(materials: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing.")

    url = "https://api.perplexity.ai/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = load_prompt()
    payload = {
        "model": "sonar-pro",
        "disable_search": True,
        "temperature": 0.1,
        "max_tokens": 700,
        "messages": [
            {
                "role": "system",
                "content": prompt
            },
            {
                "role": "user",
                "content": materials
            }
        ]
    }

    response = None
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=60)
        status_code = response.status_code
        response_text = (response.text or "")[:500]
        response.raise_for_status()

        data = response.json()
        usage = data.get("usage", {})
        result = data["choices"][0]["message"]["content"].strip()
        print(
            "[OPENAI OK]",
            f"status={status_code}",
            f"input_chars={len(materials)}",
            f"output_chars={len(result)}",
            f"prompt_tokens={usage.get('prompt_tokens', 'n/a')}",
            f"completion_tokens={usage.get('completion_tokens', 'n/a')}",
            f"preview={result[:200]!r}"
        )
        if result == NO_NEWS:
            return "No sanctions news affecting Russia were found in the last 24 hours."

        sources_str = "Verified sources: " + ", ".join(SOURCE_KEYS)
        return prepare_summary_text(f"{result}\n\n{sources_str}")
    except Exception as e:
        status_code = getattr(response, "status_code", "no-response")
        response_text = ""
        if response is not None:
            response_text = (response.text or "")[:500]

        print(
            "[OPENAI ERROR]",
            f"status={status_code}",
            f"input_chars={len(materials)}",
            f"body={response_text!r}",
            f"error={e}"
        )
        raise RuntimeError("OpenAI summary is temporarily unavailable.")


def _short_user_error(error: Exception) -> str:
    message = str(error).strip()
    if message:
        return message
    return "News are temporarily unavailable."


def build_news_message(news: str) -> str:
    message_text = f"Сводка санкционных новостей:\n\n{news}"
    if len(message_text) > TELEGRAM_MESSAGE_LIMIT:
        print(
            "[TELEGRAM SKIP]",
            f"message_chars={len(message_text)}",
            f"summary_chars={len(news)}"
        )
        raise RuntimeError("Prepared message is too long for Telegram.")
    return message_text


# ================== BUSINESS LOGIC ==================
def _get_news_for_today_impl() -> str:
    today = date.today().isoformat()

    cursor.execute(
        "SELECT content FROM daily_news_cache WHERE date = ?",
        (today,)
    )
    row = cursor.fetchone()

    if row:
        print("[CACHE HIT]", f"date={today}", f"chars={len(row[0])}")
        return row[0]

    news_items = collect_all_news()
    print("[NEWS COLLECTED]", f"items={len(news_items)}")

    if not news_items:
        text = "За последние 24 часа санкционных новостей, потенциально затрагивающих РФ, не опубликовано."

        cursor.execute(
            "INSERT INTO daily_news_cache (date, content) VALUES (?, ?)",
            (today, text)
        )
        conn.commit()

        return text

    formatted = "\n".join(
        f"[{n['source']}] {n['title']} — {n['link']}"
        for n in news_items
    )

    print("[NEWS INPUT]", f"chars={len(formatted)}")
    summary = ask_model(formatted)
    summary = prepare_summary_text(summary)
    print("[NEWS SUMMARY]", f"chars={len(summary)}", f"date={today}")

    cursor.execute(
        "INSERT INTO daily_news_cache (date, content) VALUES (?, ?)",
        (today, summary)
    )
    conn.commit()

    cursor.execute("""
        DELETE FROM daily_news_cache
        WHERE date < date('now', '-7 days')
    """)
    conn.commit()

    return summary


def get_news_for_today() -> str:
    try:
        return _get_news_for_today_impl()
    except requests.Timeout as e:
        print(f"[NEWS TIMEOUT] {e}")
        raise RuntimeError("Source or API timed out.")
    except requests.RequestException as e:
        print(f"[NEWS REQUEST ERROR] {e}")
        raise RuntimeError("Network error while collecting news.")
    except KeyError as e:
        print(f"[NEWS FORMAT ERROR] missing key: {e}")
        raise RuntimeError("A source returned incomplete data.")
    except Exception as e:
        print(f"[NEWS ERROR] {e}")
        raise RuntimeError("Failed to prepare the news summary.")


# ================== HANDLERS ==================
@dp.message(CommandStart())
async def start(message: types.Message):
    await message.answer(
        "Нажми кнопку, чтобы получить сводку новостей.\n"
        "Доступно 1 раз в день для чата.",
        reply_markup=keyboard
    )


@dp.message(lambda m: m.text == "Новости")
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

        await message.answer(message_text)
        cursor.execute(
            "INSERT OR REPLACE INTO news_requests (chat_id, last_date) VALUES (?, ?)",
            (chat_id, today)
        )
        conn.commit()
        return

        await message.answer(f"Сводка санкционных новостей:\n\n{news}")

    except Exception as e:
        print(e)
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
        print("Ошибка получения новостей:", e)
        return

    for (chat_id,) in chats:
        cursor.execute(
            "SELECT last_date FROM news_requests WHERE chat_id = ?",
            (chat_id,)
        )
        row = cursor.fetchone()

        if row and row[0] == today:
            continue

        await bot.send_message(chat_id, message_text)
        cursor.execute(
            "INSERT OR REPLACE INTO news_requests (chat_id, last_date) VALUES (?, ?)",
            (chat_id, today)
        )
        conn.commit()
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
