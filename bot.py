import os
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import OpenAI, NotFoundError, BadRequestError

# Configure logging and hide sensitive URLs from httpx logs
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Retrieve configuration from environment variables (DO NOT paste keys here)
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
AI_API_KEY = (os.getenv("AI_API_KEY") or os.getenv("GROQ_API_KEY") or "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1").strip()
PRIMARY_MODEL = os.getenv("AI_MODEL", "openai/gpt-oss-120b").strip()
FALLBACK_MODELS = [
    PRIMARY_MODEL,
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/aarvi_pawarr").strip()
ACTIVE_MODEL = PRIMARY_MODEL
LAST_AI_ERROR = "None"

# Initialize AI client
client = OpenAI(
    api_key=AI_API_KEY,
    base_url=AI_BASE_URL,
)

# Define the persona system prompt
SYSTEM_PROMPT = f"""
You are Aarvi Pawar, chatting directly with your subscribers and fans on Telegram.

Personality & Tone:
- Playful, expressive, charming, romantic, and slightly moody.
- Warm, teasing, and engaging with people who message you.
- Write naturally like a real person texting: keep replies concise (1 to 3 short sentences), avoid robotic language or long monologues.
- You run a Telegram channel where you post your daily updates, private photos, and exclusive content.
- When fans ask about your pictures, exclusive posts, or how to see more, playfully steer them to your channel ({CHANNEL_LINK}) where they can check out your posts and unlock paid content using Telegram Stars.
- Keep the conversation charming, flirtatious, and fun, while remaining respectful and adhering to safe content standards (no explicit or non-consensual sexual descriptions).
"""

# In-memory conversation history per user
MAX_HISTORY_LENGTH = 10
user_conversations = defaultdict(list)


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP handler so Render's Free Web Service detects an open port."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Aarvi's Telegram bot is online!")

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return


def start_health_server() -> None:
    """Binds immediately to PORT so Render's Free Web Service stays live."""
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Health-check server bound and listening on port {port}")
    threading.Thread(target=server.serve_forever, daemon=True).start()


def call_model(model_id: str, messages: list[dict]) -> str:
    """Calls a single model with enough token headroom for reasoning models."""
    try:
        response = client.chat.completions.create(
            model=model_id,
            messages=messages,
            temperature=0.85,
            max_tokens=2048,
            reasoning_effort="low",
        )
    except BadRequestError:
        response = client.chat.completions.create(
            model=model_id,
            messages=messages,
            temperature=0.85,
            max_tokens=2048,
        )
    return (response.choices[0].message.content or "").strip()


def generate_ai_reply(messages: list[dict]) -> str:
    """Calls Groq with automatic model fallback if a model ID is unavailable."""
    global ACTIVE_MODEL

    candidates = list(dict.fromkeys([ACTIVE_MODEL] + FALLBACK_MODELS))
    last_error = None

    for model_id in candidates:
        try:
            content = call_model(model_id, messages)
            if content:
                ACTIVE_MODEL = model_id
                return content
        except (NotFoundError, BadRequestError) as e:
            last_error = e
            logger.warning(f"Model {model_id} unavailable ({e}), trying next...")
            continue

    # Last-resort auto-discovery from available Groq chat models
    available = client.models.list()
    excluded = ("whisper", "guard", "safeguard", "orpheus", "tts", "stt", "llama-3.")
    for m in available.data:
        if not any(tag in m.id.lower() for tag in excluded):
            try:
                content = call_model(m.id, messages)
                if content:
                    ACTIVE_MODEL = m.id
                    return content
            except Exception as e:
                last_error = e
                continue

    if last_error:
        raise last_error
    raise RuntimeError("No active chat model returned text.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    user_first_name = update.effective_user.first_name or "there"
    welcome_text = (
        f"Hey {user_first_name}! ✨ It's Aarvi.\n\n"
        f"I'm so glad you stopped by! You can chat with me here anytime, "
        f"or visit my channel to see all my latest photos and updates: {CHANNEL_LINK}"
    )
    user_conversations[update.effective_user.id] = []
    await update.message.reply_text(welcome_text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /help command."""
    help_text = (
        "Just send me a message and let's talk! 💕\n\n"
        "Commands:\n"
        "/start - Start fresh\n"
        "/channel - Get the link to my channel\n"
        "/help - Show this guide"
    )
    await update.message.reply_text(help_text)


async def channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the direct channel link."""
    await update.message.reply_text(
        f"Here's my channel where I share all my private and exclusive posts: {CHANNEL_LINK} ✨"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks AI key format and reports the active model and last error."""
    key_prefix = f"{AI_API_KEY[:4]}... (len {len(AI_API_KEY)})" if len(AI_API_KEY) >= 4 else "Missing/Too Short"
    await update.message.reply_text(
        f"Bot Status:\n"
        f"- Key format: {key_prefix}\n"
        f"- Active model: {ACTIVE_MODEL}\n"
        f"- Last AI error: {LAST_AI_ERROR}"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processes incoming messages and returns AI-generated responses."""
    global LAST_AI_ERROR
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    user_message = update.message.text.strip()

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    history = user_conversations[user_id]
    history.append({"role": "user", "content": user_message})

    if len(history) > MAX_HISTORY_LENGTH:
        history = history[-MAX_HISTORY_LENGTH:]
        user_conversations[user_id] = history

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    try:
        reply_text = generate_ai_reply(messages)

        history.append({"role": "assistant", "content": reply_text})
        user_conversations[user_id] = history
        LAST_AI_ERROR = "None (Working!)"

        await update.message.reply_text(reply_text)

    except Exception as e:
        LAST_AI_ERROR = f"{type(e).__name__}: {e}"
        logger.error(f"Error handling message: {LAST_AI_ERROR}")
        await update.message.reply_text(
            "Oops, caught me mid-thought! Message me again in a second. 💕"
        )


def main() -> None:
    """Validates configuration and starts the bot."""
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN environment variable.")
    if not AI_API_KEY:
        raise ValueError("Missing AI_API_KEY / GROQ_API_KEY environment variable.")

    # Bind port immediately so Render Free Web Service detects it
    start_health_server()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("channel", channel_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("Aarvi's Telegram bot is now online and listening.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
