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
        news = get_news()
        # print(news)
        cursor.execute(
            "INSERT OR REPLACE INTO news_requests (chat_id, last_date) VALUES (?, ?)",
            (chat_id, today)
        )
        conn.commit()

        await message.answer(f"Сводка новостей:\n\n{news}")

    except Exception:
        await message.answer("Не удалось получить новости")


async def send_daily_news():
    today = date.today().isoformat()

    cursor.execute("SELECT chat_id FROM chats")
    chats = cursor.fetchall()

    for (chat_id,) in chats:
        cursor.execute(
            "SELECT last_date FROM news_requests WHERE chat_id = ?",
            (chat_id,)
        )
        row = cursor.fetchone()

        if row and row[0] == today:
            continue  # уже отправляли

        try:
            news = get_news()

            cursor.execute(
                "INSERT OR REPLACE INTO news_requests (chat_id, last_date) VALUES (?, ?)",
                (chat_id, today)
            )
            conn.commit()

            await bot.send_message(
                chat_id,
                f"Сводка санкционных новостей:\n\n{news}"
            )

        except Exception as e:
            print(f"Ошибка для чата {chat_id}: {e}")


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
