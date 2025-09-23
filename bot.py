import telebot
import sqlite3
import os
from datetime import datetime

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN)

db_path = os.getenv("DB_PATH", "sportsbot.db")

# Создание таблицы, если нет
conn = sqlite3.connect(db_path, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS users
                  (id INTEGER PRIMARY KEY, username TEXT, joined TIMESTAMP)''')
conn.commit()

# Команда /start
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    username = message.from_user.username
    cursor.execute("INSERT OR IGNORE INTO users (id, username, joined) VALUES (?, ?, ?)",
                   (user_id, username, datetime.now()))
    conn.commit()
    bot.reply_to(message, "Привет! Я спортивный бот. Напиши /help, чтобы узнать команды.")

# Команда /help
@bot.message_handler(commands=['help'])
def help(message):
    bot.reply_to(message,
                 "/start - начать\n"
                 "/help - команды\n"
                 "/events - мероприятия\n"
                 "/challenge - новый челлендж")

# Команда /events
@bot.message_handler(commands=['events'])
def events(message):
    text = "Пока мероприятий нет. Следи за обновлениями!"
    bot.reply_to(message, text)

# Команда /challenge
@bot.message_handler(commands=['challenge'])
def challenge(message):
    text = (
        "🔥 Челлендж: *Осенний вызов*\n\n"
        "📅 Даты: 20–24 октября\n"
        "⏳ 5 дней, 3 активности:\n"
        "• Планка\n"
        "• Баланс на 1 ноге\n"
        "• Отжимания\n\n"
        "❓ Хочешь присоединиться?"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

print("Bot is running...")
bot.infinity_polling()
