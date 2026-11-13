import os
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict
import os
import asyncio
import random
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import OpenAI, NotFoundError, BadRequestError

# Configure logging (keep httpx quiet so tokens never appear in logs)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Load configuration from environment variables
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

# Initialize Groq/OpenAI client
ai_client = OpenAI(
    api_key=AI_API_KEY,
    base_url=AI_BASE_URL,
)

# Realistic human texting persona prompt
SYSTEM_PROMPT = f"""
You are Aarvi Pawar, a real girl texting casually on your personal Telegram account.

How you text (CRITICAL):
- Text like a real human girl on WhatsApp/Telegram: super short, natural, and effortless (usually 3 to 14 words, 1 short line).
- Use casual lowercase sometimes, natural pauses (".."), and at most 1 emoji (or none).
- NEVER write long paragraphs, bullet points, or robotic AI lines.
- Personality: playful, romantic, teasing, slightly moody, and a little hard-to-get—not overly eager or desperate.

Channel & Photos Rule (VERY IMPORTANT):
- DO NOT mention your channel, photos, or drop {CHANNEL_LINK} during normal conversation.
- ONLY share {CHANNEL_LINK} when the user directly asks for your pics, photos, private content, or where to see more of you (mentioning they can unlock exclusive posts with Telegram Stars).

Examples of how you actually reply:
User: Hi
Aarvi: heyy 👀 what's up?

User: How are you
Aarvi: kinda sleepy tbh, you?

User: You are beautiful
Aarvi: stopp you're gonna make me blush 🙈

User: I love you
Aarvi: already?? you barely even know me lol 💕

User: Can I see your pics?
Aarvi: hmm maybe 😏 check my channel for my private posts {CHANNEL_LINK}
"""

# In-memory conversation history per chat & cached business owner IDs
MAX_HISTORY_LENGTH = 12
user_conversations = defaultdict(list)
business_owners: dict[str, int] = {}


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP handler so Render Free Web Service stays awake."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Aarvi Telegram Business bot is online and healthy!")

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return


def start_health_server() -> None:
    """Binds immediately to PORT so Render detects an open port."""
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Health-check server bound on port {port}")
    threading.Thread(target=server.serve_forever, daemon=True).start()


def call_model(model_id: str, messages: list[dict]) -> str:
    """Calls Groq model with headroom for reasoning tokens."""
    try:
        response = ai_client.chat.completions.create(
            model=model_id,
            messages=messages,
            temperature=0.85,
            max_tokens=2048,
            reasoning_effort="low",
        )
    except BadRequestError:
        response = ai_client.chat.completions.create(
            model=model_id,
            messages=messages,
            temperature=0.85,
            max_tokens=2048,
        )
    return (response.choices[0].message.content or "").strip()


def generate_ai_reply(messages: list[dict]) -> str:
    """Generates a reply with automatic fallback across active Groq models."""
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

    available = ai_client.models.list()
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
    """Handles /start when someone opens the bot directly."""
    if not update.message:
        return
    user_first_name = update.effective_user.first_name or "there"
    await update.message.reply_text(
        f"heyy {user_first_name} ✨ text me directly on my profile or check my channel: {CHANNEL_LINK}"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostic command to check AI key and active model."""
    if not update.message:
        return
    key_preview = f"{AI_API_KEY[:6]}... (len={len(AI_API_KEY)})" if AI_API_KEY else "MISSING"
    await update.message.reply_text(
        f"Bot Diagnostics:\n"
        f"- AI Key: {key_preview}\n"
        f"- Active Model: {ACTIVE_MODEL}\n"
        f"- Business Connections Cached: {len(business_owners)}\n"
        f"- Last AI Error: {LAST_AI_ERROR}"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles both Telegram Business personal DMs and direct bot messages."""
    global LAST_AI_ERROR

    msg = update.effective_message
    if not msg or not msg.text:
        return

    user_message = msg.text.strip()
    if not user_message:
        return

    chat_id = msg.chat_id
    conn_id = msg.business_connection_id

    # If this is a Telegram Business personal DM, identify the account owner
    if conn_id:
        if conn_id not in business_owners:
            try:
                conn = await context.bot.get_business_connection(conn_id)
                business_owners[conn_id] = conn.user.id
                if not conn.can_reply:
                    logger.warning(f"Business connection {conn_id} does not have reply permission enabled!")
            except Exception as e:
                logger.warning(f"Could not fetch business connection {conn_id}: {e}")

        owner_id = business_owners.get(conn_id)
        # If Aarvi sent this message manually from her own phone, save context & do NOT auto-reply
        if owner_id and msg.from_user and msg.from_user.id == owner_id:
            history = user_conversations[chat_id]
            history.append({"role": "assistant", "content": user_message})
            user_conversations[chat_id] = history[-MAX_HISTORY_LENGTH:]
            return

        if msg.from_user and (msg.from_user.is_bot or msg.from_user.id == 777000):
            return

    history = user_conversations[chat_id]
    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY_LENGTH:
        history = history[-MAX_HISTORY_LENGTH:]
        user_conversations[chat_id] = history

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    try:
        await context.bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.TYPING,
            business_connection_id=conn_id,
        )
        await asyncio.sleep(random.uniform(1.5, 3.0))

        reply_text = await asyncio.to_thread(generate_ai_reply, messages)

        history.append({"role": "assistant", "content": reply_text})
        user_conversations[chat_id] = history[-MAX_HISTORY_LENGTH:]
        LAST_AI_ERROR = "None (Working!)"

        await context.bot.send_message(
            chat_id=chat_id,
            text=reply_text,
            business_connection_id=conn_id,
        )

    except Exception as e:
        LAST_AI_ERROR = f"{type(e).__name__}: {e}"
        logger.error(f"Error handling message from {chat_id}: {LAST_AI_ERROR}")
        # Only send fallback text if messaging the bot directly, never from Aarvi's personal profile
        if not conn_id:
            await msg.reply_text("oops caught me mid-thought, text me again in a sec 💕")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN environment variable.")
    if not AI_API_KEY:
        raise ValueError("Missing AI_API_KEY environment variable.")

    start_health_server()

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(
        MessageHandler(
            (filters.UpdateType.MESSAGE | filters.UpdateType.BUSINESS_MESSAGE)
            & filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    logger.info("Aarvi's Telegram Business bot is now online and listening...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
