import os
import logging
import re
import json
from datetime import datetime, timedelta
from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)
from groq import Groq

# ─── Настройки ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")

MODEL          = "llama-3.3-70b-versatile"   # бесплатная мощная модель
MAX_HISTORY    = 20                           # сколько сообщений помнит бот
SYSTEM_PROMPT  = (
    "Ты — умный и дружелюбный ассистент Марина (женщина). "
    "Отвечай кратко и по делу. "
    "Если не знаешь ответа — честно скажи об этом."
)

# ─── Инициализация ────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)
REMIND_KEYWORDS = ["напомни", "напоминай", "remind", "напомнить"]

# Хранилище истории: { chat_id: [ {role, content}, ... ] }
conversation_history: dict[int, list[dict]] = {}


# ─── Вспомогательные функции ──────────────────────────────────────────────────
def get_history(chat_id: int) -> list[dict]:
    return conversation_history.setdefault(chat_id, [])


def add_to_history(chat_id: int, role: str, content: str):
    history = get_history(chat_id)
    history.append({"role": role, "content": content})
    # Обрезаем историю, оставляя последние MAX_HISTORY сообщений
    if len(history) > MAX_HISTORY:
        conversation_history[chat_id] = history[-MAX_HISTORY:]


def ask_groq(chat_id: int, user_text: str) -> str:
    add_to_history(chat_id, "user", user_text)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + get_history(chat_id)

    response = groq_client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=1024,
    )

    answer = response.choices[0].message.content
    add_to_history(chat_id, "assistant", answer)
    return answer


# ─── Команды ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        f"Привет, {name}! 👋\n\n"
        "Я AI-ассистент на базе Llama 3.3 (Groq).\n"
        "Просто напиши мне что-нибудь, и я отвечу.\n\n"
        "📌 Команды:\n"
        "/start — это сообщение\n"
        "/clear — очистить историю диалога\n"
        "/help  — помощь\n"
        "/about — о боте"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_user.id
    conversation_history.pop(chat_id, None)
    await update.message.reply_text("🗑️ История диалога очищена. Начнём заново!")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💡 Как пользоваться ботом:\n\n"
        "• Просто пиши любые вопросы или задачи\n"
        "• Бот помнит последние 20 сообщений диалога\n"
        "• /clear — если хочешь начать новую тему\n\n"
        "Примеры запросов:\n"
        "— Объясни квантовую физику простыми словами\n"
        "— Напиши функцию на Python для сортировки\n"
        "— Придумай 5 идей для подарка другу"
    )


async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 О боте:\n\n"
        f"Модель: {MODEL}\n"
        "Провайдер: Groq (бесплатный tier)\n"
        f"Память: последние {MAX_HISTORY} сообщений\n\n"
        "Groq даёт бесплатный доступ к Llama 3.3 70B — "
        "одной из лучших открытых моделей."
    )


# ─── Обработчик сообщений ─────────────────────────────────────────────────────
def parse_reminder(text: str) -> dict | None:
    """Просим Groq распарсить напоминание, возвращает {seconds, reminder_text} или None"""
    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[{
                "role": "user",
                "content": (
                    f"Текущее время и дата: {datetime.now().strftime('%Y-%m-%d %H:%M')}.\n"
                    f"Из этого сообщения извлеки время напоминания и текст напоминания: '{text}'\n"
                    "Пользователь может указать:\n"
                    "- через сколько времени: 'через 30 минут', 'через 2 часа'\n"
                    "- конкретное время сегодня: 'в 18:00', 'в 9 утра'\n"
                    "- конкретную дату и время: '25 мая в 15:00', 'завтра в 10:00'\n"
                    "Ответь ТОЛЬКО валидным JSON без пояснений и markdown:\n"
                    '{"seconds": <число секунд от текущего момента до напоминания>, "reminder_text": "<текст напоминания>"}\n'
                    "Если seconds получается отрицательным — добавь 24 часа (86400 секунд).\n"
                    "Если не можешь распарсить — ответь: {\"error\": \"cant parse\"}"
                )
            }],
            temperature=0,
            max_tokens=100,
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        data = json.loads(raw)
        if "error" in data or "seconds" not in data:
            return None
        return data
    except Exception as e:
        logger.error(f"Ошибка парсинга напоминания: {e}")
        return None


async def send_reminder(context):
    """Отправляет напоминание пользователю"""
    job = context.job
    chat_id = job.data["chat_id"]
    user_id = job.data["user_id"]
    username = job.data.get("username")
    reminder_text = job.data["reminder_text"]
    
    if username:
        text = f"⏰ @{username}, напоминание: {reminder_text}"
    else:
        text = f"⏰ <a href='tg://user?id={user_id}'>{job.data['first_name']}</a>, напоминание: {reminder_text}"
    
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список активных напоминаний"""
    user_id = update.effective_user.id
    jobs = context.job_queue.get_jobs_by_name(str(user_id))
    
    if not jobs:
        await update.message.reply_text("У тебя нет активных напоминаний.")
        return
    
    text = "⏰ Твои напоминания:\n\n"
    for i, job in enumerate(jobs, 1):
        seconds_left = int((job.next_t - datetime.now(job.next_t.tzinfo)).total_seconds())
        minutes = seconds_left // 60
        hours = minutes // 60
        if hours > 0:
            time_str = f"через {hours} ч {minutes % 60} мин"
        elif minutes > 0:
            time_str = f"через {minutes} мин"
        else:
            time_str = f"через {seconds_left} сек"
        text += f"{i}. {job.data['reminder_text']} — {time_str}\n"
    
    text += "\nЧтобы отменить напиши /cancel 1 (или другой номер)"
    await update.message.reply_text(text)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет напоминание по номеру"""
    user_id = update.effective_user.id
    jobs = context.job_queue.get_jobs_by_name(str(user_id))
    
    if not jobs:
        await update.message.reply_text("У тебя нет активных напоминаний.")
        return
    
    try:
        num = int(context.args[0]) - 1
        if num < 0 or num >= len(jobs):
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(f"Укажи номер от 1 до {len(jobs)}. Например: /cancel 1")
        return
    
    reminder_text = jobs[num].data['reminder_text']
    jobs[num].schedule_removal()
    await update.message.reply_text(f"✅ Напоминание отменено: {reminder_text}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = update.message.text or ""
    bot_username = context.bot.username

    logger.info(f"Сообщение: '{user_text}'")
    logger.info(f"Тип чата: {update.effective_chat.type}")
    logger.info(f"Reply to: {update.message.reply_to_message}")
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        logger.info(f"Reply from username: {update.message.reply_to_message.from_user.username}")
    logger.info(f"Bot username: {bot_username}")

    # В группах реагируем на упоминание или цитирование сообщений бота
    if update.effective_chat.type in ["group", "supergroup"]:
        is_mention = f"@{bot_username}" in user_text
        is_reply_to_bot = (
            update.message.reply_to_message is not None
            and update.message.reply_to_message.from_user is not None
            and update.message.reply_to_message.from_user.username == bot_username
        )
        if not is_mention and not is_reply_to_bot:
            return
        # Убираем упоминание из текста чтобы не путать AI
        user_text = user_text.replace(f"@{bot_username}", "").strip()

    # Если текст пустой — просим уточнить
    if not user_text:
        await update.message.reply_text("Напиши что ты хочешь узнать 😊")
        return
	
    # Проверяем не напоминание ли это
    if any(kw in user_text.lower() for kw in REMIND_KEYWORDS):
        parsed = parse_reminder(user_text)
        if parsed:
            seconds = int(parsed["seconds"])
            reminder_text = parsed["reminder_text"]
            context.job_queue.run_once(
                send_reminder,
                when=seconds,
                data={
                    "chat_id": update.effective_chat.id,
                    "user_id": update.effective_user.id,
                    "username": update.effective_user.username,
                    "first_name": update.effective_user.first_name or "друг",
                    "reminder_text": reminder_text,
                },
                name=str(update.effective_user.id),
            )
            minutes = seconds // 60
            hours = minutes // 60
            if hours > 0:
                time_str = f"{hours} ч {minutes % 60} мин"
            elif minutes > 0:
                time_str = f"{minutes} мин"
            else:
                time_str = f"{seconds} сек"
            await update.message.reply_text(f"✅ Напомню через {time_str}: {reminder_text}")
            return
        else:
            await update.message.reply_text("⚠️ Не смог распознать время. Попробуй например: 'напомни через 30 минут позвонить маме'")
            return
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        answer = ask_groq(chat_id, user_text)
        await update.message.reply_text(answer)
    except Exception as e:
        logger.error(f"Ошибка Groq: {e}")
        await update.message.reply_text(
            "⚠️ Произошла ошибка при обращении к AI. "
            "Попробуй ещё раз через несколько секунд."
        )

# ─── Запуск ───────────────────────────────────────────────────────────────────
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Начать / главное меню"),
        BotCommand("clear", "Очистить историю диалога"),
        BotCommand("help",  "Помощь"),
        BotCommand("about", "О боте"),
	BotCommand("reminders", "Список активных напоминаний"),
        BotCommand("cancel", "Отменить напоминание — /cancel 1"),
    ])


def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
