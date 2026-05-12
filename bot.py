import os
import asyncio
import logging
import re
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
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

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
REMIND_KEYWORDS  = ["напомни", "remind",]
IMAGE_KEYWORDS   = ["нарисуй", "сгенерируй", "draw", "нарисовать", "сгенерировать"]
SUMMARY_KEYWORDS = ["перескажи", "пересказ", "summarize", "кратко", "о чём", "о чем"]

# Хранилище истории: { user_id: [ {role, content}, ... ] }
conversation_history: dict[int, list[dict]] = {}

# Кэш таймзон чтобы не ходить в БД на каждое сообщение
timezone_cache: dict[int, int] = {}

# Глобальный пул соединений с БД
db_pool = None


# ─── База данных (asyncpg) ────────────────────────────────────────────────────
async def init_db():
    global db_pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL не задан — таймзоны не будут сохраняться между перезапусками")
        return
    try:
        import asyncpg
        db_pool = await asyncpg.create_pool(DATABASE_URL, ssl="require")
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY,
                    timezone_offset INTEGER DEFAULT 3
                )
            """)
        logger.info("БД инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        db_pool = None

async def get_timezone_db(user_id: int) -> int:
    if db_pool is None:
        return 3
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT timezone_offset FROM user_settings WHERE user_id = $1", user_id
            )
            return row["timezone_offset"] if row else 3
    except Exception as e:
        logger.error(f"Ошибка чтения таймзоны: {e}")
        return 3

async def set_timezone_db(user_id: int, offset: int):
    if db_pool is None:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_settings (user_id, timezone_offset)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET timezone_offset = $2
            """, user_id, offset)
    except Exception as e:
        logger.error(f"Ошибка записи таймзоны: {e}")

def get_timezone(user_id: int) -> int:
    return timezone_cache.get(user_id, 3)

async def load_timezone(user_id: int) -> int:
    if user_id not in timezone_cache:
        timezone_cache[user_id] = await get_timezone_db(user_id)
    return timezone_cache[user_id]


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

async def search_web(query: str) -> str:
    """Ищет в интернете через Tavily"""
    if not TAVILY_API_KEY:
        return ""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": TAVILY_API_KEY,
                    "query": query,
                    "max_results": 5,
                    "search_depth": "basic",
                    "include_answer": True,
                }
            )
            data = resp.json()
            parts = []
            if data.get("answer"):
                parts.append(f"Краткий ответ: {data['answer']}")
            for r in data.get("results", [])[:3]:
                parts.append(f"— {r['title']}: {r['content'][:300]}")
            return "\n".join(parts)
    except Exception as e:
        logger.error(f"Ошибка поиска Tavily: {e}")
        return ""

def needs_search(user_text: str) -> bool:
    """Определяет нужен ли поиск в интернете"""
    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[{
                "role": "user",
                "content": (
                    f"Нужен ли поиск в интернете чтобы ответить на этот вопрос: '{user_text}'?\n"
                    "Отвечай ТОЛЬКО 'да' или 'нет'.\n"
                    "Поиск нужен если вопрос про: актуальные новости, текущие события, курсы валют, погоду, "
                    "цены, расписания, результаты матчей, свежие данные, что произошло недавно.\n"
                    "Поиск НЕ нужен для: общих знаний, математики, объяснений, советов, творческих задач."
                )
            }],
            temperature=0,
            max_tokens=5,
        )
        return "да" in response.choices[0].message.content.strip().lower()
    except Exception:
        return False

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

async def ask_groq_with_search(chat_id: int, user_text: str) -> str:
    """Отвечает с поиском если нужно"""
    if TAVILY_API_KEY and needs_search(user_text):
        logger.info(f"Поиск в интернете для: '{user_text}'")
        search_results = await search_web(user_text)
        if search_results:
            add_to_history(chat_id, "user", user_text)
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + get_history(chat_id)[:-1] + [{
                "role": "user",
                "content": f"{user_text}\n\n[Результаты поиска]:\n{search_results}\n\nОтветь на вопрос используя эти данные."
            }]
            response = groq_client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0.7,
                max_tokens=1024,
            )
            answer = response.choices[0].message.content
            add_to_history(chat_id, "assistant", answer)
            return answer
    return ask_groq(chat_id, user_text)


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

async def cmd_8ball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import random
    answers = [
        "Однозначно да! 🟢",
        "Без сомнений — да! 🟢",
        "Мои источники говорят да 🟢",
        "Всё указывает на это 🟢",
        "Скорее всего да 🟡",
        "Хороший знак 🟡",
        "Спроси позже 🟡",
        "Сложно сказать — попробуй снова 🟡",
        "Не рассчитывай на это 🔴",
        "Мой ответ — нет 🔴",
        "Мои источники говорят нет 🔴",
        "Перспективы не очень 🔴",
        "Очень сомнительно 🔴",
    ]
    if not context.args:
        await update.message.reply_text("🎱 Задай вопрос! Например: /8ball стоит ли мне сделать бочку?")
        return
    question = " ".join(context.args)
    answer = random.choice(answers)
    await update.message.reply_text(f"🎱 Вопрос: {question}\n\nОтвет: {answer}")

async def cmd_random(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import random
    if not context.args:
        await update.message.reply_text(
            "🎲 Укажи варианты через запятую чтобы:\n\n"
            "1) Выбрать одно: /random A, B, C\n"
            "2) Выбрать несколько: /random # A, B, C" 
        )
        return

    text = " ".join(context.args)

    # Проверяем есть ли число в начале — сколько выбрать
    count = 1
    match = re.match(r'^(\d+)\s+', text)
    if match:
        count = int(match.group(1))
        text = text[match.end():]

    options = [o.strip() for o in text.split(",") if o.strip()]

    if len(options) < 2:
        await update.message.reply_text("Нужно минимум 2 варианта через запятую!")
        return

    if count > len(options):
        await update.message.reply_text(f"Вариантов всего {len(options)}, не могу выбрать {count}!")
        return

    chosen = random.sample(options, count)

    if count == 1:
        phrases = [
            "Ну давай,", "Я думаю,", "Пожалуй,", "Хм, наверное,",
            "Я бы выбрала", "Однозначно", "Если честно,", "Окей, пусть будет", "Я за", "Мой выбор —",
        ]
        phrase = random.choice(phrases)
        await update.message.reply_text(f"🎲 {phrase} {chosen[0]}!")
    else:
        result = "\n".join(f"{i+1}. {c}" for i, c in enumerate(chosen))
        await update.message.reply_text(f"🎲 Выбираю {count} из {len(options)}...\n\n{result}")

async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        current = await load_timezone(user_id)
        await update.message.reply_text(
            f"🕐 Твой часовой пояс: UTC+{current}\n\n"
            "Чтобы изменить напиши /remindertimezone #\n"
            "Примеры:\n Москва = 3,\n Екатеринбург = 5,\n Новосибирск = 7,\n Владивосток = 10"
        )
        return
    try:
        offset = int(context.args[0])
        if offset < -12 or offset > 14:
            raise ValueError
        timezone_cache[user_id] = offset
        await set_timezone_db(user_id, offset)
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
    text += "\nЧтобы отменить напиши /remindercancel # "
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
        await update.message.reply_text(f"Укажи номер от 1 до {len(jobs)}. Например: /remindercancel 1")
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
            tomorrow_start = datetime(now.year, now.month, now.day, 0, 0, 0) + timedelta(days=1)
            target = tomorrow_start.replace(hour=hour, minute=minute)
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


# ─── Пересказ текста/статьи ───────────────────────────────────────────────────
async def summarize_text(update: Update, text: str):
    """Пересказывает текст через Groq"""
    if len(text) > 12000:
        text = text[:12000]
    await update.message.reply_chat_action("typing")
    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[{
                "role": "user",
                "content": (
                    f"Сделай краткий пересказ следующего текста на русском языке. "
                    f"Выдели главные мысли, факты и выводы. Отвечай кратко и по делу:\n\n{text}"
                )
            }],
            temperature=0.5,
            max_tokens=1024,
        )
        summary = response.choices[0].message.content.strip()
        await update.message.reply_text(f"📝 Ну, значит, смотри:\n\n{summary}")
    except Exception as e:
        logger.error(f"Ошибка пересказа: {e}")
        await update.message.reply_text("⚠️ Не удалось сделать пересказ, попробуй ещё раз.")

async def fetch_and_summarize(update: Update, url: str):
    """Скачивает страницу и пересказывает"""
    import httpx
    from html.parser import HTMLParser

    class TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.text_parts = []
            self.skip_tags = {'script', 'style', 'head', 'nav', 'footer', 'aside'}
            self.current_skip = False
            self.skip_depth = 0

        def handle_starttag(self, tag, attrs):
            if tag in self.skip_tags:
                self.current_skip = True
                self.skip_depth += 1

        def handle_endtag(self, tag):
            if tag in self.skip_tags:
                self.skip_depth -= 1
                if self.skip_depth <= 0:
                    self.current_skip = False
                    self.skip_depth = 0

        def handle_data(self, data):
            if not self.current_skip:
                stripped = data.strip()
                if stripped:
                    self.text_parts.append(stripped)

        def get_text(self):
            return " ".join(self.text_parts)

    await update.message.reply_text("👀 Читаю статью...")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0"
        }) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        extractor = TextExtractor()
        extractor.feed(html)
        text = extractor.get_text()

        if len(text) < 100:
            await update.message.reply_text("⚠️ Не удалось извлечь текст со страницы — возможно сайт за пейволлом или требует авторизации.")
            return

        await summarize_text(update, text)
    except Exception as e:
        logger.error(f"Ошибка загрузки страницы: {e}")
        await update.message.reply_text("⚠️ Не удалось загрузить страницу. Проверь ссылку или попробуй ещё раз.")

async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /summary — работает с ссылкой в аргументах или с цитатой"""
    # Ссылка передана напрямую: /summary https://...
    if context.args:
        url = context.args[0]
        if url.startswith("http"):
            await fetch_and_summarize(update, url)
            return
        else:
            await update.message.reply_text("Укажи полную ссылку начиная с https://")
            return

    # Цитата с ссылкой или текстом
    if update.message.reply_to_message:
        quoted = update.message.reply_to_message
        # Берём текст или подпись к фото/видео
        quoted_text = quoted.text or quoted.caption or ""

        # Ищем ссылку в тексте или подписи
        url_in_quote = re.search(r'https?://\S+', quoted_text)
        if url_in_quote:
            await fetch_and_summarize(update, url_in_quote.group(0))
            return

        # Проверяем entities на ссылки (кликабельные ссылки без текста)
        entities = quoted.entities or quoted.caption_entities or []
        for entity in entities:
            if entity.type == "url":
                url = quoted_text[entity.offset:entity.offset + entity.length]
                await fetch_and_summarize(update, url)
                return
            if entity.type == "text_link" and entity.url:
                await fetch_and_summarize(update, entity.url)
                return

        if quoted_text:
            await summarize_text(update, quoted_text)
            return

        await update.message.reply_text("В цитате нет текста. Если это фото — текст должен быть в подписи к нему.")
        return

    await update.message.reply_text(
        "📝 Перескажу содержание статьи на сайте или текста в тг:\n\n"
        "1) Процитируй сообщение с текстом или ссылкой и напиши: /summary\n"
        "2) Или в новом сообщении добавь ссылку на статью: /summary https://ссылка\n\n ⚠️ Не сработает, если на сайте есть пейволл или защита от ботов"
	
    )



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


# ─── Распознавание фото ───────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    bot_username = context.bot.username
    caption = update.message.caption or ""

    await load_timezone(user_id)

    # В группах реагируем только если тегают или цитируют бота
    if update.effective_chat.type in ["group", "supergroup"]:
        is_mention = f"@{bot_username}" in caption
        is_reply_to_bot = (
            update.message.reply_to_message is not None
            and update.message.reply_to_message.from_user is not None
            and update.message.reply_to_message.from_user.username == bot_username
        )
        if not is_mention and not is_reply_to_bot:
            return
        caption = caption.replace(f"@{bot_username}", "").strip()

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        # Скачиваем фото
        photo = update.message.photo[-1]  # берём максимальное разрешение
        file = await context.bot.get_file(photo.file_id)
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(file.file_path)
            image_data = resp.content

        import base64
        image_b64 = base64.b64encode(image_data).decode("utf-8")

        # Отправляем в vision модель
        prompt = caption if caption else "Опиши что на этом фото подробно на русском языке."

        response = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}"
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }],
            max_tokens=1024,
        )
        answer = response.choices[0].message.content
        await update.message.reply_text(answer)

    except Exception as e:
        logger.error(f"Ошибка распознавания фото: {e}")
        await update.message.reply_text("⚠️ Не удалось обработать фото, попробуй ещё раз.")


# ─── Обработчик сообщений ─────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = update.message.text or ""
    bot_username = context.bot.username
    user_id = update.effective_user.id

    # Загружаем таймзону в кэш если ещё не загружена
    await load_timezone(user_id)

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

    # Рандомайзер через текст
    if any(kw in user_text.lower() for kw in ["выбери из", "выбери", "выбирай"]):
        import random
        clean_text = user_text
        for kw in ["выбери из", "выбери", "выбирай"]:
            clean_text = re.sub(kw, "", clean_text, flags=re.IGNORECASE)
        clean_text = clean_text.strip(" ,.")
        options = [o.strip() for o in re.split(r'[,]', clean_text) if o.strip()]
        options = [o for o in options if re.search(r'[a-zA-Zа-яА-ЯёЁ0-9]', o)]
        if len(options) >= 2:
            phrases = [
                "Ну давай,", "Я думаю,", "Может,", "Пожалуй,", "Хм, наверное,",
                "Я бы выбрала", "Однозначно", "Без вопросов —", "Ну смотри,",
                "Если честно,", "Окей, пусть будет", "Я за", "Мой выбор —",
            ]
            phrase = random.choice(phrases)
            chosen = random.choice(options)
            await update.message.reply_text(f"🎲 {phrase} {chosen}!")
            return
        else:
            await update.message.reply_text(
                "Укажи варианты через запятую!\n"
                "Например: выбери пицца, суши, бургер"
            )
            return

    # Пересказ
    if any(kw in user_text.lower() for kw in SUMMARY_KEYWORDS):
        # Ссылка в тексте сообщения
        url_match = re.search(r'https?://\S+', user_text)
        if url_match:
            await fetch_and_summarize(update, url_match.group(0))
            return
        # Цитата с текстом или ссылкой
        if update.message.reply_to_message:
            quoted = update.message.reply_to_message
            quoted_text = quoted.text or quoted.caption or ""       
            url_in_quote = re.search(r'https?://\S+', quoted_text)
            if url_in_quote:
                await fetch_and_summarize(update, url_in_quote.group(0))
                return
            if len(quoted_text) > 50:
                await summarize_text(update, quoted_text)
                return
        await update.message.reply_text(
            "Процитируй текст или сообщение со ссылкой и напиши 'перескажи',\n"
            "или используй /summary https://ссылка"
        )
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
            await update.message.reply_text("С удовольствием. Что ты хочешь, чтобы я тебе нарисовала?")
        return

    # Напоминания
    if any(kw in user_text.lower() for kw in REMIND_KEYWORDS):
        parsed = parse_reminder(user_text, user_id)
        if parsed:
            seconds = int(parsed["seconds"])
            reminder_text = parsed["reminder_text"]
            context.job_queue.run_once(
                send_reminder,
                when=seconds,
                data={
                    "chat_id": update.effective_chat.id,
                    "user_id": user_id,
                    "username": update.effective_user.username,
                    "first_name": update.effective_user.first_name or "друг",
                    "reminder_text": reminder_text,
                },
                name=str(user_id),
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
        answer = await ask_groq_with_search(user_id, user_text)
        await update.message.reply_text(answer)
    except Exception as e:
        logger.error(f"Ошибка Groq: {e}")
        await update.message.reply_text("⚠️ Произошла ошибка при обращении к AI. Попробуй ещё раз через несколько секунд.")


# ─── Запуск ───────────────────────────────────────────────────────────────────
async def post_init(app):
    await init_db()
    await app.bot.set_my_commands(
        [
            BotCommand("start",     "Начать / главное меню"),
            BotCommand("clear",     "Очистить историю диалога"),
            BotCommand("reminderlist", "Активные напоминания"),
            BotCommand("remindercancel",    "Отменить напоминание"),
            BotCommand("remindertimezone",  "Установить часовой пояс для напоминаний"),
            BotCommand("help",      "Помощь"),
            BotCommand("about",     "О боте"),
            BotCommand("8ball",     "Магический шар"),
            BotCommand("random",    "Выбрать случайный вариант"),
            BotCommand("summary",   "Пересказ текста или статьи"),
        ],
        scope=BotCommandScopeDefault()
    )
    await app.bot.set_my_commands(
        [
            BotCommand("remindertimezone",  "Установить часовой пояс для напоминаний"),
            BotCommand("reminderlist", "Активные напоминания"),
            BotCommand("remindercancel",    "Отменить напоминание"),
            BotCommand("8ball",     "Магический шар"),
            BotCommand("random",    "Выбрать случайный вариант"),
            BotCommand("summary",   "Пересказ текста или статьи"),
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

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("clear",     cmd_clear))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("about",     cmd_about))
    app.add_handler(CommandHandler("reminderlist",     cmd_reminders))
    app.add_handler(CommandHandler("remindercancel",   cmd_cancel))
    app.add_handler(CommandHandler("remindertimezone", cmd_timezone))
    app.add_handler(CommandHandler("8ball",     cmd_8ball))
    app.add_handler(CommandHandler("random",    cmd_random))
    app.add_handler(CommandHandler("summary",   cmd_summary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Бот запущен...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
