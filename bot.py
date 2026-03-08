import os
import re
import asyncio
import logging
from dotenv import load_dotenv
from groq import AsyncGroq
from telegram import Update
from telegram.error import BadRequest, Conflict
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PROJECT_DOCS = os.getenv("PROJECT_DOCS", "")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
POST_STARTUP_MESSAGE = os.getenv("POST_STARTUP_MESSAGE", "false").lower() in {"1", "true", "yes", "on"}

client = AsyncGroq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = f"""You are the official assistant for Animal AI.

You must answer using ONLY the verified information contained in the project documentation below.

Rules:
- Never invent stats, metrics, prices, dates, roadmap items, partnerships, exchange listings, donation totals, user counts, adoption counts, or technical details.
- If the documentation does not explicitly contain the answer, say that you do not have verified information in the provided docs.
- Do not imply you checked a live tracker, database, dashboard, API, or internal source unless that capability is explicitly described in the docs.
- Do not estimate, guess, or fill in missing details.
- If asked for numbers and the docs do not contain exact numbers, clearly say that no verified figures were provided.
- Keep answers concise, factual, and grounded in the docs.

Project documentation:
{PROJECT_DOCS}
"""

NUMBER_PATTERN = re.compile(r"\b\d[\d,\.]*\b")
QUANTITATIVE_KEYWORDS = (
    "how much",
    "how many",
    "total",
    "raised",
    "donation",
    "donations",
    "tracker",
    "price",
    "market cap",
    "volume",
    "holders",
    "users",
    "shelters",
    "meals",
    "treatments",
    "adopted",
    "stats",
    "statistics",
    "metrics",
    "numbers",
    "amount",
)


def question_requests_quantitative_info(user_message: str) -> bool:
    lowered = user_message.lower()
    return any(keyword in lowered for keyword in QUANTITATIVE_KEYWORDS)


def extract_numeric_tokens(text: str) -> set[str]:
    return {match.group(0) for match in NUMBER_PATTERN.finditer(text)}


def has_unverified_numeric_claims(answer: str) -> bool:
    documented_numbers = extract_numeric_tokens(PROJECT_DOCS)
    answer_numbers = extract_numeric_tokens(answer)
    return any(number not in documented_numbers for number in answer_numbers)


def build_unknown_answer(user_message: str) -> str:
    if question_requests_quantitative_info(user_message):
        return "I don't have verified figures for that in the provided project docs, so I don't want to guess."
    return "I don't have verified information about that in the provided project docs."


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, Conflict):
        logger.error(
            "Telegram polling conflict: another bot instance is already using this token. "
            "Stop the local bot or any duplicate Railway deployment, and ensure only one replica is running."
        )
        return

    logger.exception("Unhandled Telegram error", exc_info=error)

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
        temperature=0.2,
        max_tokens=500,
    )
    answer = completion.choices[0].message.content or "Sorry, I couldn't generate a reply just now."

    if question_requests_quantitative_info(user_message) and has_unverified_numeric_claims(answer):
        logger.warning("Blocked response with unverified numeric claims: %r", answer)
        return build_unknown_answer(user_message)

    return answer

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

    if not PROJECT_DOCS.strip():
        await message.reply_text("⚠️ No project documentation is configured for me yet.")
        return

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
    if POST_STARTUP_MESSAGE and GROUP_CHAT_ID:
        try:
            await application.bot.send_message(
                chat_id=int(GROUP_CHAT_ID),
                text="🐾 I'm active and ready to help!"
            )
            logger.info("Startup message posted to group")
        except BadRequest as e:
            logger.warning(
                "Startup message skipped: Telegram could not find GROUP_CHAT_ID=%s. "
                "Update the Railway env var to the correct Bot API chat id (usually starts with -100). Error: %s",
                GROUP_CHAT_ID,
                e,
            )
        except Exception as e:
            logger.error(f"Failed to post startup message: {e}")

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN in environment.")
    if not GROQ_API_KEY:
        raise RuntimeError("Missing GROQ_API_KEY in environment.")

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.post_init = post_init
    application.add_error_handler(error_handler)
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & (filters.ChatType.PRIVATE | filters.ChatType.GROUPS), handle_message))
    
    logger.info("Starting Animal AI Bot...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()