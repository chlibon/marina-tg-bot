import os
import logging
import re
import json
import urllib.parse
from datetime import datetime, timedelta
from telegram import Update, BotCommand, BotCommandScopeDefault, BotCommandScopeAllGroupChats
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
DATABASE_URL   = os.getenv("DATABASE_URL", "")

MODEL          = "llama-3.3-70b-versatile"
MAX_HISTORY    = 20
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
IMAGE_KEYWORDS  = ["нарисуй", "сгенерируй", "draw", "нарисовать", "сгенерировать"]

# Хранилище истории: { user_id: [ {role, content}, ... ] }
conversation_history: dict[int, list[dict]] = {}


# ─── База данных ──────────────────────────────────────────────────────────────
def get_db():
    import psycopg2
    url = urllib.parse.urlparse(DATABASE_URL)
    return psycopg2.connect(
        host=url.hostname,
        port=url.port or 5432,
        dbname=url.path.lstrip("/"),
        user=url.username,
        password=url.password,
        sslmode="require",
    )

def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                timezone_offset INTEGER DEFAULT 3
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("БД инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")

def get_timezone(user_id: int) -> int:
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT timezone_offset FROM user_settings WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else 3
    except Exception:
        return 3

def set_timezone(user_id: int, offset: int):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_settings (user_id, timezone_offset)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET timezone_offset = %s
        """, (user_id, offset, offset))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка записи таймзоны: {e}")


# ─── Вспомогательные функции ──────────────────────────────────────────────────
def get_user_now(user_id: int) -> datetime:
    offset = get_timezone(user_id)
    return datetime.utcnow() + timedelta(hours=offset)

def get_history(chat_id: int) -> list[dict]:
    return conversation_history.setdefault(chat_id, [])

def add_to_history(chat_id: int, role: str, content: str):
    history = get_history(chat_id)
    history.append({"role": role, "content": content})
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
        current = get_timezone(user_id)
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
        set_timezone(user_id, offset)
        await update.message.reply_text(f"✅ Часовой пояс установлен: UTC+{offset}")
    except ValueError:
        await update.message.reply_text("Укажи число от -12 до 14. Например: /timezone 5")

async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


# ─── Напоминания ──────────────────────────────────────────────────────────────
def parse_reminder(text: str, user_id: int) -> dict | None:
    now = get_user_now(user_id)
    logger.info(f"parse_reminder: now={now}, user_id={user_id}, offset={get_timezone(user_id)}")
    seconds = None

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

    # Завтра в HH:MM — проверяем ДО просто "в HH:MM"
    if seconds is None:
        match = re.search(r'завтра\s+в\s+(\d{1,2}):(\d{2})', text.lower())
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            tomorrow = now.date() + timedelta(days=1)
            target = datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, minute)
            seconds = int((target - now).total_seconds())

    # Дата + время — "25 мая в 15:00" — тоже ДО просто "в HH:MM"
    months_names = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']
    months = {m: i+1 for i, m in enumerate(months_names)}
    has_month = any(m in text.lower() for m in months_names)

    if seconds is None and has_month:
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

    # Конкретное время — в HH:MM (только если нет завтра и нет месяца)
    if seconds is None and 'завтра' not in text.lower() and not has_month:
        match = re.search(r'в\s+(\d{1,2}):(\d{2})', text.lower())
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            today = now.date()
            target = datetime(today.year, today.month, today.day, hour, minute)
            if target <= now:
                target = datetime(today.year, today.month, today.day + 1, hour, minute)
            seconds = int((target - now).total_seconds())

    # Полночь
    if seconds is None and 'полноч' in text.lower():
        today = now.date()
        target = datetime(today.year, today.month, today.day, 0, 0)
        if target <= now:
            target += timedelta(days=1)
        seconds = int((target - now).total_seconds())

    # Полдень
    if seconds is None and 'полден' in text.lower():
        today = now.date()
        target = datetime(today.year, today.month, today.day, 12, 0)
        if target <= now:
            target += timedelta(days=1)
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
                    "Убери слова: напомни, remind, через X минут/часов, в HH:MM, завтра, полночь, полдень, числа месяцев.\n"
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


# ─── Генерация картинок ───────────────────────────────────────────────────────
async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    chat_id = update.effective_chat.id
    try:
        translation = groq_client.chat.completions.create(
            model=MODEL,
            messages=[{
                "role": "user",
                "content": f"Переведи этот запрос на английский язык для генерации изображения, ответь ТОЛЬКО переводом без пояснений: '{prompt}'"
            }],
            temperature=0,
            max_tokens=100,
        )
        english_prompt = translation.choices[0].message.content.strip()
    except Exception:
        english_prompt = prompt

    await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
    try:
        import httpx
        encoded = urllib.parse.quote(english_prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true"
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url)
            response.raise_for_status()
            image_bytes = response.content
        await update.message.reply_photo(photo=image_bytes, caption=f"🎨 {prompt}")
    except Exception as e:
        logger.error(f"Ошибка генерации картинки: {e}")
        await update.message.reply_text("⚠️ Не удалось сгенерировать картинку, попробуй ещё раз.")


# ─── Обработчик сообщений ─────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = update.message.text or ""
    bot_username = context.bot.username

    logger.info(f"Сообщение: '{user_text}'")
    logger.info(f"Тип чата: {update.effective_chat.type}")
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        logger.info(f"Reply from username: {update.message.reply_to_message.from_user.username}")

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
        user_text = user_text.replace(f"@{bot_username}", "").strip()

    if not user_text:
        await update.message.reply_text("Напиши что ты хочешь узнать 😊")
        return

    # Генерация картинок
    if any(kw in user_text.lower() for kw in IMAGE_KEYWORDS):
        image_prompt = user_text.lower()
        for kw in IMAGE_KEYWORDS:
            image_prompt = image_prompt.replace(kw, "")
        image_prompt = image_prompt.strip(" ,.")
        if image_prompt:
            await generate_image(update, context, image_prompt)
        else:
            await update.message.reply_text("Напиши что нарисовать, например: нарисуй закат над морем")
        return

    # Напоминания
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
        answer = ask_groq(update.effective_user.id, user_text)
        await update.message.reply_text(answer)
    except Exception as e:
        logger.error(f"Ошибка Groq: {e}")
        await update.message.reply_text("⚠️ Произошла ошибка при обращении к AI. Попробуй ещё раз через несколько секунд.")


# ─── Запуск ───────────────────────────────────────────────────────────────────
async def post_init(app):
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
    await app.bot.set_my_commands(
        [
            BotCommand("timezone",  "Установить часовой пояс для напоминаний — /timezone #"),
            BotCommand("reminders", "Список активных напоминаний"),
            BotCommand("cancel",    "Отменить напоминание — /cancel #"),
        ],
        scope=BotCommandScopeAllGroupChats()
    )


def main():
    init_db()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("clear",     cmd_clear))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("about",     cmd_about))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("timezone",  cmd_timezone))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
