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


# ================== ENV ==================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")

# ================== TELEGRAM ==================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Новости")]],
    resize_keyboard=True
)

# ================== DATABASE (SQLite) ==================
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

# ================== PROMPT ==================
def load_prompt():
    prompt_path = Path(__file__).parent / "prompt.txt"
    return prompt_path.read_text(encoding="utf-8")


# ================== PERPLEXITY ==================
def get_news():
    url = "https://api.perplexity.ai/chat/completions"

    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }

    PROMPT = load_prompt()

    payload = {
        "model": "sonar-pro",
        "messages": [
            {
                "role": "user",
                "content": PROMPT
            }
        ],
        "temperature": 0
    }

    # Test API ping
    test_payload = {
        "model": "sonar",
        "messages": [
            {
                "role": "user",
                "content": "ping"
            }
        ],
        "max_tokens": 1
    }

    response = requests.post(url, json=payload, headers=headers, timeout=40)
    response.raise_for_status()
    print(response.json())
    return response.json()["choices"][0]["message"]["content"]


def is_monday() -> bool:
    return date.today().weekday() == 0  # Monday


def get_weekly_cache():
    cursor.execute("""
        SELECT date, content
        FROM daily_news_cache
        WHERE date >= date('now', '-7 days')
        ORDER BY date ASC
    """)
    return cursor.fetchall()


def cleanup_old_cache():
    cursor.execute("""
        DELETE FROM daily_news_cache
        WHERE date < date('now', '-7 days')
    """)
    conn.commit()


def get_news_for_today() -> str:
    today = date.today().isoformat()

    if is_monday():
        weekly = get_weekly_cache()
        today_news = get_news()

        parts = ["Сводка санкционных новостей за прошлую неделю:\n"]

        for d, text in weekly:
            parts.append(f"{d}\n{text}\n")

        parts.append("Обновления за последние 24 часа:\n")
        parts.append(today_news)

        final_text = "\n".join(parts)

        # очищаем всё старше недели
        cleanup_old_cache()

        # сохраняем только сегодняшний день
        cursor.execute(
            "INSERT OR REPLACE INTO daily_news_cache (date, content) VALUES (?, ?)",
            (today, today_news)
        )
        conn.commit()

        return final_text

    # if not Monday
    cursor.execute(
        "SELECT content FROM daily_news_cache WHERE date = ?",
        (today,)
    )
    row = cursor.fetchone()

    if row:
        return row[0]

    news = get_news()

    cursor.execute(
        "INSERT INTO daily_news_cache (date, content) VALUES (?, ?)",
        (today, news)
    )
    conn.commit()

    cleanup_old_cache()
    return news


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
            "Новости уже публиковались сегодня.\n"
            "Теперь они будут приходить автоматически."
        )
        return

    await message.answer("Собираю новости...")

    try:
        news = get_news_for_today()

        cursor.execute(
            "INSERT OR REPLACE INTO news_requests (chat_id, last_date) VALUES (?, ?)",
            (chat_id, today)
        )
        conn.commit()

        await message.answer(f"Сводка новостей:\n\n{news}")

    except Exception as e:
        print(e)
        await message.answer("Не удалось получить новости")



async def send_daily_news():
    today = date.today().isoformat()

    cursor.execute("SELECT chat_id FROM chats")
    chats = cursor.fetchall()

    try:
        news = get_news_for_today()
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

        cursor.execute(
            "INSERT OR REPLACE INTO news_requests (chat_id, last_date) VALUES (?, ?)",
            (chat_id, today)
        )
        conn.commit()

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
        hour=9,
        minute=0
    )

    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
