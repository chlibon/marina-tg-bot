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

# Хранилище часовых поясов: { user_id: int }
user_timezones: dict[int, int] = {}

def get_user_now(user_id: int) -> datetime:
    """Возвращает текущее время для пользователя с учётом его часового пояса"""
    offset = user_timezones.get(user_id, 3)  # по умолчанию UTC+3 (Москва)
    # datetime.utcnow() — всегда UTC независимо от сервера
    return datetime.utcnow() + timedelta(hours=offset)


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

async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        current = user_timezones.get(user_id, 3)
        await update.message.reply_text(
            f"🕐 Твой часовой пояс: UTC+{current}\n\n"
            "Чтобы изменить напиши /timezone 5 (для Екатеринбурга)\n"
            "Примеры: Москва = 3, Екб = 5, Новосибирск = 7, Владивосток = 10"
        )
        return
    try:
        offset = int(context.args[0])
        if offset < -12 or offset > 14:
            raise ValueError
        user_timezones[user_id] = offset
        await update.message.reply_text(f"✅ Часовой пояс установлен: UTC+{offset}")
    except ValueError:
        await update.message.reply_text("Укажи число от -12 до 14. Например: /timezone 5")


# ─── Обработчик сообщений ─────────────────────────────────────────────────────
def parse_reminder(text: str, user_id: int) -> dict | None:
    """Парсит время из текста напоминания"""
    now = get_user_now(user_id)
    seconds = None

    logger.info(f"parse_reminder: now={now}, user_id={user_id}, offset={user_timezones.get(user_id, 3)}")
    
    # Относительное время — через X минут/часов/дней
    match = re.search(r'через\s+(\d+)\s*(секунд|минут|час|часа|часов|день|дня|дней)', text.lower())
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if 'секунд' in unit:
            seconds = amount
        elif 'минут' in unit:
            seconds = amount * 60
        elif 'час' in unit:
            seconds = amount * 3600
        elif 'ден' in unit or 'день' in unit or 'дня' in unit:
            seconds = amount * 86400

    # Полночь
    if seconds is None and 'полноч' in text.lower():
        target = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        seconds = int((target - now).total_seconds())

    # Полдень
    if seconds is None and 'полден' in text.lower():
        target = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        seconds = int((target - now).total_seconds())


    # Завтра в HH:MM — ОБЯЗАТЕЛЬНО ДО блока "в HH:MM"
    if seconds is None:
        match = re.search(r'завтра\s+в\s+(\d{1,2}):(\d{2})', text.lower())
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            tomorrow = now.date() + timedelta(days=1)
            target = datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, minute)
            seconds = int((target - now).total_seconds())

    # Конкретное время — в HH:MM (только если нет слова "завтра")
    if seconds is None and 'завтра' not in text.lower():
        match = re.search(r'в\s+(\d{1,2}):(\d{2})', text.lower())
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            today = now.date()
            target = datetime(today.year, today.month, today.day, hour, minute)
            if target <= now:
                target = datetime(today.year, today.month, today.day + 1, hour, minute)
            seconds = int((target - now).total_seconds())

    # Дата + время — "25 мая в 15:00"
    if seconds is None:
        months = {
            'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4,
            'мая': 5, 'июня': 6, 'июля': 7, 'августа': 8,
            'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12
        }
        match = re.search(
            r'(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+в\s+(\d{1,2}):(\d{2})',
            text.lower()
        )
        if match:
            day = int(match.group(1))
            month = months[match.group(2)]
            hour = int(match.group(3))
            minute = int(match.group(4))
            year = now.year
            target = datetime(year, month, day, hour, minute)
            if target <= now:
                target = datetime(year + 1, month, day, hour, minute)
            seconds = int((target - now).total_seconds())

    if seconds is None:
        return None

    # Groq только для текста напоминания
    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[{
                "role": "user",
                "content": (
                    f"Из этого сообщения извлеки только текст напоминания (без указания времени): '{text}'\n"
                    "Перефразируй от лица бота: 'напомни мне сделать X' → 'сделать X', "
                    "'напомни чтобы я позвонил' → 'чтобы ты позвонил'.\n"
                    "Убери слова: напомни, remind, через X минут/часов, в HH:MM, завтра, полночь, полдень.\n"
                    "Ответь ТОЛЬКО текстом напоминания, без пояснений."
                )
            }],
            temperature=0,
            max_tokens=100,
        )
        reminder_text = response.choices[0].message.content.strip()
    except Exception:
        reminder_text = text

    return {"seconds": seconds, "reminder_text": reminder_text}


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
        parsed = parse_reminder(user_text, update.effective_user.id)
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
    from telegram.constants import BotCommandScopeType
    from telegram import BotCommandScopeDefault, BotCommandScopeAllGroupChats

    # Команды для лички
    await app.bot.set_my_commands(
        [
            BotCommand("start",     "Начать / главное меню"),
            BotCommand("clear",     "Очистить историю диалога"),
            BotCommand("reminders", "Список активных напоминаний"),
            BotCommand("cancel",    "Отменить напоминание — /cancel #"),
            BotCommand("timezone",  "Установить часовой пояс для напоминаний — /timezone #"),
            BotCommand("help",      "Помощь"),
            BotCommand("about",     "О боте"),
        ],
        scope=BotCommandScopeDefault()
    )

    # Команды для групп — только самое нужное
    await app.bot.set_my_commands(
        [
	    BotCommand("timezone", "Установить часовой пояс для напоминаний — /timezone #"),
	    BotCommand("reminders", "Список активных напоминаний"),
            BotCommand("cancel",   "Отменить напоминание — /cancel #"),
            
        ],
        scope=BotCommandScopeAllGroupChats()
    )


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
    app.add_handler(CommandHandler("timezone", cmd_timezone))

    logger.info("Бот запущен...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
