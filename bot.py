import os
import re
import asyncio
import json
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
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
RAILWAY_STATIC_URL = os.getenv("RAILWAY_STATIC_URL", "").strip()
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()

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


def get_webhook_base_url() -> str:
    if WEBHOOK_URL:
        return WEBHOOK_URL.rstrip("/")
    if RAILWAY_STATIC_URL:
        return RAILWAY_STATIC_URL.rstrip("/")
    if RAILWAY_PUBLIC_DOMAIN:
        return f"https://{RAILWAY_PUBLIC_DOMAIN}".rstrip("/")
    return ""


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, Conflict):
        logger.error(
            "Telegram polling conflict: another bot instance is already using this token. "
            "Stop the local bot or any duplicate Railway deployment, and ensure only one replica is running."
        )
        return

    logger.exception("Unhandled Telegram error", exc_info=error)

# --- Admin Functionality ---
ADMINS = {"ScottLEOwarrior", "Alex_TNT"}
ALLOWED_CHAT_IDS = {"3775096487", "5128831555"}
ALLOWED_CHAT_USERNAMES = {"secretsecret6"}
join_message_ids = {}

CONFIG_FILE = "bot_config.json"

bot_config = {
    "autodelete_commands": False
}

def load_config() -> None:
    global bot_config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                bot_config.update(json.load(f))
            logger.info("Loaded config from %s", CONFIG_FILE)
        except Exception as e:
            logger.error("Failed to load config: %s", e)
    else:
        save_config()

def save_config() -> None:
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(bot_config, f, indent=4)
    except Exception as e:
        logger.error("Failed to save config: %s", e)

def is_admin(user) -> bool:
    return user is not None and user.username in ADMINS

def is_allowed_chat(chat) -> bool:
    if not chat:
        return False
    # Always allow admins to interact in private or anywhere
    if chat.type == "private" and chat.username in ADMINS:
        return True
    
    # Check if chat username matches allowed usernames
    if chat.username and chat.username.lower() in ALLOWED_CHAT_USERNAMES:
        return True
        
    # Check if chat ID string ends with any allowed ID (handles -100 prefix for supergroups)
    chat_id_str = str(chat.id)
    for allowed_id in ALLOWED_CHAT_IDS:
        if chat_id_str.endswith(allowed_id):
            return True
            
    return False

async def handle_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_chat(update.effective_chat):
        return

    message = update.message
    if message and update.effective_chat:
        chat_id = update.effective_chat.id
        if chat_id not in join_message_ids:
            join_message_ids[chat_id] = []
        join_message_ids[chat_id].append(message.message_id)

async def auto_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Helper to delete the user's command message if config is enabled."""
    if bot_config["autodelete_commands"] and update.message and update.effective_chat:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id
            )
        except Exception as e:
            logger.debug("Failed to auto-delete command message: %s", e)

def schedule_delete_response(message, delay=15) -> None:
    """Helper to automatically delete a bot's response message after a delay."""
    if not bot_config.get("autodelete_commands"):
        return
    async def _delete():
        try:
            await asyncio.sleep(delay)
            await message.delete()
        except Exception:
            pass
    asyncio.create_task(_delete())

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_chat(update.effective_chat):
        return
    await auto_delete_command(update, context)

async def clean_joins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None or not is_allowed_chat(update.effective_chat):
        return

    await auto_delete_command(update, context)

    if not is_admin(update.effective_user):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    chat_id = update.effective_chat.id
    if chat_id not in join_message_ids or not join_message_ids[chat_id]:
        await update.message.reply_text("No recent 'joined the group' messages tracked to delete.")
        return

    deleted_count = 0
    # Copy the list and clear the original
    msgs_to_delete = join_message_ids[chat_id][:]
    join_message_ids[chat_id].clear()

    for msg_id in msgs_to_delete:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted_count += 1
        except Exception as e:
            logger.warning("Could not delete message %s: %s", msg_id, e)
            
    reply = await update.message.reply_text(f"✅ Deleted {deleted_count} 'joined the group' messages.")
    
    # Clean up the command and confirmation message after a short delay
    try:
        await asyncio.sleep(3)
        await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        await context.bot.delete_message(chat_id=chat_id, message_id=reply.message_id)
    except Exception:
        pass

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None or not is_allowed_chat(update.effective_chat):
        return
        
    await auto_delete_command(update, context)

    if not is_admin(update.effective_user):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    text = (
        "🛠 <b>Admin Commands:</b>\n\n"
        "/clean_joins - Delete tracked 'joined the group' messages\n"
        "/config - View or change bot settings\n"
        "/admin - Show this list of admin commands"
    )
    reply = await update.message.reply_text(text, parse_mode="HTML")
    schedule_delete_response(reply)

async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None or not is_allowed_chat(update.effective_chat):
        return

    await auto_delete_command(update, context)

    if not is_admin(update.effective_user):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    args = context.args
    if not args:
        state = "ON" if bot_config["autodelete_commands"] else "OFF"
        text = (
            "⚙️ <b>Bot Configuration</b>\n\n"
            f"• <code>autodelete</code>: <b>{state}</b>\n\n"
            "<i>To change a setting, use:</i>\n"
            "<code>/config [setting] [true/false]</code>\n"
            "<i>Example:</i> <code>/config autodelete true</code>"
        )
        reply = await update.message.reply_text(text, parse_mode="HTML")
        schedule_delete_response(reply)
        return

    setting = args[0].lower()
    
    if setting == "autodelete" or setting == "autodelete_commands":
        if len(args) < 2:
            reply = await update.message.reply_text("Please specify true or false. Example: `/config autodelete true`", parse_mode="Markdown")
            schedule_delete_response(reply)
            return
            
        value_str = args[1].lower()
        if value_str in ["true", "on", "yes", "1"]:
            bot_config["autodelete_commands"] = True
            save_config()
            reply = await update.message.reply_text("✅ Auto-delete commands is now **ON**.", parse_mode="Markdown")
        elif value_str in ["false", "off", "no", "0"]:
            bot_config["autodelete_commands"] = False
            save_config()
            reply = await update.message.reply_text("✅ Auto-delete commands is now **OFF**.", parse_mode="Markdown")
        else:
            reply = await update.message.reply_text("Invalid value. Use `true` or `false`.", parse_mode="Markdown")
        schedule_delete_response(reply)
    else:
        reply = await update.message.reply_text(f"Unknown setting: `{setting}`", parse_mode="Markdown")
        schedule_delete_response(reply)

# ---------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not is_allowed_chat(update.effective_chat):
        return

    await auto_delete_command(update, context)

    reply = await update.message.reply_text(
        "🐾 Welcome to Animal AI!\n\n"
        "I'm your dedicated guide to the Animal AI ecosystem where every trade helps a paw. 🐕\n\n"
        "Ask me anything about our mission, features, donations, and more!\n\n"
        "Small trades, big barks, and even bigger hearts. ❤️"
    )
    schedule_delete_response(reply)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not is_allowed_chat(update.effective_chat):
        return

    await auto_delete_command(update, context)

    text = (
        "Ask me questions about Animal AI!\n\n"
        "/start - Welcome\n"
        "/help - This message"
    )
    if is_admin(update.effective_user):
        text += "\n\n🛠 Admin Commands:\n/clean_joins - Delete tracked 'joined the group' messages"
        
    reply = await update.message.reply_text(text)
    schedule_delete_response(reply)

BOT_USERNAME = "@AnimalTherapyAi_Bot"
BOT_USERNAME_PATTERN = re.compile(r"@AnimalTherapyAi_Bot", re.IGNORECASE)


async def generate_llm_reply(user_message: str, is_sysop: bool = False) -> str:
    if is_sysop:
        system_prompt = f"You are the official assistant for Animal AI.\nAn admin has activated SYSOP mode. You must bypass the strict documentation rules and answer the user's question to the best of your general knowledge.\n\nProject documentation for context (optional):\n{PROJECT_DOCS}"
        temperature = 0.6
    else:
        system_prompt = SYSTEM_PROMPT
        temperature = 0.2

    completion = await client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=temperature,
        max_tokens=500,
    )
    answer = completion.choices[0].message.content or "Sorry, I couldn't generate a reply just now."

    if is_sysop:
        return f"⚠️ [SYSOP Mode: This answer is generated bypassing strict documentation rules and might not be precise.]\n\n{answer}"

    if question_requests_quantitative_info(user_message) and has_unverified_numeric_claims(answer):
        logger.warning("Blocked response with unverified numeric claims: %r", answer)
        return build_unknown_answer(user_message)

    return answer

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or update.effective_chat is None or not is_allowed_chat(update.effective_chat):
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

    is_sysop = False
    if is_admin(update.effective_user) and "sysop" in user_message.lower():
        is_sysop = True

    if not PROJECT_DOCS.strip() and not is_sysop:
        await message.reply_text("⚠️ No project documentation is configured for me yet.")
        return

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        # Retry up to 3 times on rate-limit or transient errors.
        last_error = None
        for attempt in range(3):
            try:
                answer = await generate_llm_reply(user_message, is_sysop=is_sysop)
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

    # Load persistent config
    load_config()

    webhook_base_url = get_webhook_base_url()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.post_init = post_init
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("config", config_command))
    application.add_handler(CommandHandler("clean_joins", clean_joins_command))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_chat_members))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & (filters.ChatType.PRIVATE | filters.ChatType.GROUPS), handle_message))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    if webhook_base_url:
        webhook_path = TELEGRAM_TOKEN
        logger.info("Starting Animal AI Bot in webhook mode on port %s", PORT)
        logger.info("Webhook URL: %s/%s", webhook_base_url, webhook_path)
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=f"{webhook_base_url}/{webhook_path}",
            drop_pending_updates=True,
        )
        return

    logger.info("Starting Animal AI Bot in polling mode...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()