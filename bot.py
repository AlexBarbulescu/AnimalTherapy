import os
import re
import asyncio
import logging
from dotenv import load_dotenv
from groq import AsyncGroq
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PROJECT_DOCS = os.getenv("PROJECT_DOCS", "")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")

client = AsyncGroq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = f"""You are a helpful assistant for Animal AI, a crypto project focused on DeFi and animal charity.
Use this documentation: {PROJECT_DOCS}

Be concise and helpful."""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "🐾 Welcome to Animal AI!\n\n"
        "I'm your dedicated guide to the Animal AI ecosystem where every trade helps a paw. 🐕\n\n"
        "Ask me anything about our mission, features, donations, and more!\n\n"
        "Small trades, big barks, and even bigger hearts. ❤️"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "Ask me questions about Animal AI!\n\n"
        "/start - Welcome\n"
        "/help - This message"
    )

BOT_USERNAME = "@AnimalTherapyAi_Bot"
BOT_USERNAME_PATTERN = re.compile(r"@AnimalTherapyAi_Bot", re.IGNORECASE)


async def generate_llm_reply(user_message: str) -> str:
    completion = await client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.7,
        max_tokens=500,
    )
    return completion.choices[0].message.content or "Sorry, I couldn't generate a reply just now."

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or update.effective_chat is None:
        return

    chat_type = message.chat.type  # 'private', 'group', 'supergroup', 'channel'
    text = message.text or ""
    logger.info(f"Message received | chat_type={chat_type} | text={text!r}")

    # In group chats, only respond when the bot is mentioned
    if chat_type in ("group", "supergroup"):
        if BOT_USERNAME.lower() not in text.lower():
            return
        # Strip the mention so Gemini gets a clean question
        user_message = BOT_USERNAME_PATTERN.sub("", text).strip()
        if not user_message:
            await message.reply_text("🐾 You called? Ask me anything about Animal AI!")
            return
    else:
        user_message = text

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        # Retry up to 3 times on rate-limit or transient errors.
        last_error = None
        for attempt in range(3):
            try:
                answer = await generate_llm_reply(user_message)
                await message.reply_text(answer)
                return
            except Exception as e:
                last_error = e
                if "429" in str(e) or "rate limit" in str(e).lower() or "too many requests" in str(e).lower():
                    wait = 15 * (attempt + 1)
                    logger.warning(f"Rate limited, retrying in {wait}s (attempt {attempt + 1}/3)...")
                    await asyncio.sleep(wait)
                else:
                    raise

        if last_error is not None:
            raise last_error

        raise RuntimeError("LLM request failed without a specific error.")
    except Exception as e:
        logger.error(f"Error: {e}")
        if "429" in str(e) or "rate limit" in str(e).lower() or "too many requests" in str(e).lower():
            await message.reply_text("⏳ I'm a bit busy right now, please try again in a minute!")
        elif "api_key" in str(e).lower() or "authentication" in str(e).lower():
            await message.reply_text("⚠️ The bot's LLM API key is missing or invalid.")
        else:
            await message.reply_text("Sorry, error occurred. Try again.")

async def post_init(application: Application) -> None:
    if GROUP_CHAT_ID:
        try:
            await application.bot.send_message(
                chat_id=int(GROUP_CHAT_ID),
                text="🐾 I'm active and ready to help!"
            )
            logger.info("Startup message posted to group")
        except Exception as e:
            logger.error(f"Failed to post startup message: {e}")

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN in environment.")
    if not GROQ_API_KEY:
        raise RuntimeError("Missing GROQ_API_KEY in environment.")

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.post_init = post_init
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & (filters.ChatType.PRIVATE | filters.ChatType.GROUPS), handle_message))
    
    logger.info("Starting Animal AI Bot...")
    application.run_polling()

if __name__ == "__main__":
    main()