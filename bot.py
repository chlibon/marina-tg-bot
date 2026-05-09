import os
import logging
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
    chat_id = update.effective_chat.id
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
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = update.message.text or ""
    bot_username = context.bot.username

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
    mention_or_private = filters.ChatType.PRIVATE | filters.Entity("mention")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & mention_or_private, handle_message))

    logger.info("Бот запущен...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
