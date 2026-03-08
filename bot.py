import os
import re
import asyncio
import json
import logging
import time
from dotenv import load_dotenv
from groq import AsyncGroq
from telegram import Update
from telegram.error import BadRequest, Conflict, RetryAfter
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PROJECT_DOCS = os.getenv("PROJECT_DOCS", "")
PROJECT_DOCS_FILE = os.getenv("PROJECT_DOCS_FILE", "project_docs.txt").strip()
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
POST_STARTUP_MESSAGE = os.getenv("POST_STARTUP_MESSAGE", "false").lower() in {"1", "true", "yes", "on"}
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
RAILWAY_STATIC_URL = os.getenv("RAILWAY_STATIC_URL", "").strip()
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
POLLING_RECONNECT_DELAY = max(1, int(os.getenv("POLLING_RECONNECT_DELAY", "5")))

client = AsyncGroq(api_key=GROQ_API_KEY)
polling_recovery_task: asyncio.Task | None = None


def get_project_docs_path() -> str:
    if not PROJECT_DOCS_FILE:
        return ""
    if os.path.isabs(PROJECT_DOCS_FILE):
        return PROJECT_DOCS_FILE
    return os.path.join(BASE_DIR, PROJECT_DOCS_FILE)

def get_project_docs() -> str:
    project_docs_path = get_project_docs_path()
    if project_docs_path:
        try:
            with open(project_docs_path, "r", encoding="utf-8") as docs_file:
                docs = docs_file.read().strip()
            if docs:
                return docs
        except FileNotFoundError:
            logger.info("Project docs file %s not found; falling back to PROJECT_DOCS env var.", project_docs_path)
        except Exception as exc:
            logger.warning("Failed to read project docs file %s: %s", project_docs_path, exc)

    return PROJECT_DOCS.strip()


def build_system_prompt(project_docs: str) -> str:
    return f"""You are the official assistant for Animal AI.

You must answer using ONLY the verified information contained in the project documentation below.

Rules:
- Never invent stats, metrics, prices, dates, roadmap items, partnerships, exchange listings, donation totals, user counts, adoption counts, or technical details.
- If the documentation does not explicitly contain the answer, say that you do not have verified information in the provided docs.
- Do not imply you checked a live tracker, database, dashboard, API, or internal source unless that capability is explicitly described in the docs.
- Do not estimate, guess, or fill in missing details.
- If asked for numbers and the docs do not contain exact numbers, clearly say that no verified figures were provided.
- Keep answers concise, factual, and grounded in the docs.

Project documentation:
{project_docs}
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


def has_unverified_numeric_claims(answer: str, project_docs: str) -> bool:
    documented_numbers = extract_numeric_tokens(project_docs)
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
    global polling_recovery_task
    error = context.error
    if isinstance(error, Conflict):
        logger.error(
            "Telegram polling conflict: another bot instance is already using this token. "
            "Attempting to disconnect any stale session and reconnect this bot."
        )
        if polling_recovery_task is None or polling_recovery_task.done():
            polling_recovery_task = asyncio.create_task(recover_polling_conflict(context.application))
        return

    logger.exception("Unhandled Telegram error", exc_info=error)


async def recover_polling_conflict(application: Application) -> None:
    updater = application.updater
    if updater is None:
        logger.error("Cannot recover Telegram polling conflict because no updater is configured.")
        return

    while True:
        try:
            if updater.running:
                logger.info("Stopping current polling session before reconnecting.")
                await updater.stop()

            try:
                await application.bot.delete_webhook(drop_pending_updates=False)
            except BadRequest as exc:
                logger.debug("No webhook needed clearing before polling reconnect: %s", exc)

            logger.info("Retrying Telegram polling in %ss.", POLLING_RECONNECT_DELAY)
            await asyncio.sleep(POLLING_RECONNECT_DELAY)
            await updater.start_polling(drop_pending_updates=False)
            logger.info("Telegram polling reconnected successfully after conflict.")
            return
        except Conflict:
            logger.warning(
                "Telegram polling is still locked by another active session. Retrying in %ss.",
                POLLING_RECONNECT_DELAY,
            )
            await asyncio.sleep(POLLING_RECONNECT_DELAY)
        except Exception as exc:
            logger.exception("Failed to recover Telegram polling after conflict: %s", exc)
            return

# --- Admin Functionality ---
join_message_ids = {}

CONFIG_FILE = "bot_config.json"
MAX_PURGE_MESSAGES = 100

DEFAULT_BOT_CONFIG = {
    "autodelete_commands": False,
    "admins": ["scottleowarrior", "alex_tnt"],
    "allowed_chat_ids": ["3775096487", "5128831555"],
    "allowed_chat_usernames": ["secretsecret6"],
}

bot_config = DEFAULT_BOT_CONFIG.copy()


def _normalize_username_list(values) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        if value is None:
            continue
        username = str(value).strip()
        if username:
            normalized.append(username.lower())
    return normalized


def _normalize_chat_id_list(values) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        chat_id = str(value).strip()
        if chat_id:
            normalized.append(chat_id)
    return normalized


def _merge_config(raw_config: object) -> dict:
    merged = DEFAULT_BOT_CONFIG.copy()
    if not isinstance(raw_config, dict):
        return merged

    merged["autodelete_commands"] = bool(raw_config.get("autodelete_commands", False))
    merged["admins"] = _normalize_username_list(raw_config.get("admins", DEFAULT_BOT_CONFIG["admins"]))
    merged["allowed_chat_ids"] = _normalize_chat_id_list(raw_config.get("allowed_chat_ids", DEFAULT_BOT_CONFIG["allowed_chat_ids"]))
    merged["allowed_chat_usernames"] = _normalize_username_list(raw_config.get("allowed_chat_usernames", DEFAULT_BOT_CONFIG["allowed_chat_usernames"]))
    return merged

def load_config() -> None:
    global bot_config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                loaded_config = json.load(f)
            bot_config = _merge_config(loaded_config)
            logger.info("Loaded config from %s", CONFIG_FILE)

            if loaded_config != bot_config:
                save_config()
                logger.info("Updated %s with any missing default settings", CONFIG_FILE)
        except Exception as e:
            logger.error("Failed to load config: %s", e)
    else:
        bot_config = DEFAULT_BOT_CONFIG.copy()
        save_config()

def save_config() -> None:
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(bot_config, f, indent=4)
    except Exception as e:
        logger.error("Failed to save config: %s", e)

def is_admin(user) -> bool:
    return user is not None and bool(user.username) and user.username.lower() in bot_config.get("admins", [])


def log_admin_access_denied(update: Update, command_name: str) -> None:
    user = update.effective_user
    chat = update.effective_chat
    logger.warning(
        "Admin access denied for %s | user_id=%s username=%r chat_id=%s chat_type=%s configured_admins=%s",
        command_name,
        getattr(user, "id", None),
        getattr(user, "username", None),
        getattr(chat, "id", None),
        getattr(chat, "type", None),
        bot_config.get("admins", []),
    )

def is_allowed_chat(chat) -> bool:
    if not chat:
        return False
    # Always allow admins to interact in private or anywhere
    if chat.type == "private" and chat.username and chat.username.lower() in bot_config.get("admins", []):
        return True
    
    # Check if chat username matches allowed usernames
    if chat.username and chat.username.lower() in bot_config.get("allowed_chat_usernames", []):
        return True
        
    # Check if chat ID string ends with any allowed ID (handles -100 prefix for supergroups)
    chat_id_str = str(chat.id)
    for allowed_id in bot_config.get("allowed_chat_ids", []):
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
    """Helper to auto-delete a user's command message when enabled or sent by an admin."""
    if update.message is None or update.effective_chat is None:
        return

    should_delete = bot_config["autodelete_commands"] or is_admin(update.effective_user)
    if not should_delete:
        return

    delay = 5 if is_admin(update.effective_user) else 0

    async def _delete() -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await delete_message_with_retry(
                context,
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
        except Exception as e:
            logger.debug("Failed to auto-delete command message: %s", e)

    asyncio.create_task(_delete())

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


async def delete_message_with_retry(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, retries: int = 3) -> bool:
    attempt = 0
    while attempt <= retries:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            return True
        except RetryAfter as e:
            attempt += 1
            wait_time = float(getattr(e, "retry_after", 1) or 1)
            logger.warning(
                "Telegram rate limit while deleting message %s in chat %s. Retrying in %.2fs (%s/%s).",
                message_id,
                chat_id,
                wait_time,
                attempt,
                retries,
            )
            await asyncio.sleep(wait_time)
        except BadRequest:
            return False
        except Exception:
            raise
    return False

async def clean_joins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None or not is_allowed_chat(update.effective_chat):
        return

    await auto_delete_command(update, context)

    if not is_admin(update.effective_user):
        log_admin_access_denied(update, "/clean_joins")
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
        log_admin_access_denied(update, "/admin")
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    text = (
        "🛠 <b>Admin Commands:</b>\n\n"
        "/clean_joins - Delete tracked 'joined the group' messages\n"
        f"/purge [count] - Delete up to {MAX_PURGE_MESSAGES} recent messages\n"
        "/config - View or change bot settings\n"
        "/admin - Show this list of admin commands"
    )
    reply = await update.message.reply_text(text, parse_mode="HTML")
    schedule_delete_response(reply)

# --- Purge Functionality ---
async def purge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None or not is_allowed_chat(update.effective_chat):
        return

    if not is_admin(update.effective_user):
        log_admin_access_denied(update, "/purge")
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    chat_id = update.effective_chat.id
    if not context.args:
        reply = await update.message.reply_text(
            f"Usage: /purge <count>\nExample: /purge 20\nMaximum per run: {MAX_PURGE_MESSAGES}",
        )
        schedule_delete_response(reply)
        return

    try:
        requested_count = int(context.args[0])
    except ValueError:
        reply = await update.message.reply_text("⚠️ Please provide a valid number. Example: /purge 20")
        schedule_delete_response(reply)
        return

    if requested_count <= 0:
        reply = await update.message.reply_text("⚠️ Please provide a number greater than 0.")
        schedule_delete_response(reply)
        return

    delete_target = min(requested_count, MAX_PURGE_MESSAGES)
    if requested_count > MAX_PURGE_MESSAGES:
        status_msg = await update.message.reply_text(
            f"⚠️ Telegram-safe limit applied: deleting the most recent {MAX_PURGE_MESSAGES} messages.",
        )
        schedule_delete_response(status_msg, delay=10)

    deleted_recent_count = 0
    skipped_message_count = 0
    command_message_id = update.message.message_id
    current_message_id = command_message_id - 1
    max_scan_count = max(delete_target * 10, delete_target + 25)

    while current_message_id > 0 and deleted_recent_count < delete_target and skipped_message_count < max_scan_count:
        try:
            deleted = await delete_message_with_retry(context, chat_id=chat_id, message_id=current_message_id)
            if deleted:
                deleted_recent_count += 1
            else:
                skipped_message_count += 1
        except Exception as e:
            logger.warning("Failed to purge message %s in chat %s: %s", current_message_id, chat_id, e)
            skipped_message_count += 1
        current_message_id -= 1

    command_deleted = False
    try:
        command_deleted = await delete_message_with_retry(context, chat_id=chat_id, message_id=command_message_id)
    except Exception as e:
        logger.debug("Failed to delete purge command message: %s", e)

    if deleted_recent_count == delete_target:
        status_text = (
            f"✅ Purged {deleted_recent_count} recent message(s)"
            f"{', plus the purge command.' if command_deleted else '.'}"
        )
    else:
        status_text = (
            f"⚠️ Purged {deleted_recent_count} of {delete_target} requested recent message(s)"
            f" after scanning {deleted_recent_count + skipped_message_count} earlier message ID(s)"
            f"{', plus the purge command.' if command_deleted else '.'}"
        )

    reply = await context.bot.send_message(
        chat_id=chat_id,
        text=status_text,
    )
    schedule_delete_response(reply, delay=5)
# --------------------------------------

async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None or not is_allowed_chat(update.effective_chat):
        return

    await auto_delete_command(update, context)

    if not is_admin(update.effective_user):
        log_admin_access_denied(update, "/config")
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
        text += f"\n\n🛠 Admin Commands:\n/clean_joins - Delete tracked 'joined the group' messages\n/purge [count] - Delete up to {MAX_PURGE_MESSAGES} recent messages"
        
    reply = await update.message.reply_text(text)
    schedule_delete_response(reply)

BOT_USERNAME = "@AnimalTherapyAi_Bot"
BOT_USERNAME_PATTERN = re.compile(r"@AnimalTherapyAi_Bot", re.IGNORECASE)


async def generate_llm_reply(user_message: str, is_sysop: bool = False) -> str:
    project_docs = get_project_docs()
    if is_sysop:
        system_prompt = f"You are the official assistant for Animal AI.\nAn admin has activated SYSOP mode. You must bypass the strict documentation rules and answer the user's question to the best of your general knowledge.\n\nProject documentation for context (optional):\n{project_docs}"
        temperature = 0.6
    else:
        system_prompt = build_system_prompt(project_docs)
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

    if question_requests_quantitative_info(user_message) and has_unverified_numeric_claims(answer, project_docs):
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

    if not get_project_docs() and not is_sysop:
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
    if not get_webhook_base_url():
        try:
            await application.bot.delete_webhook(drop_pending_updates=True)
            logger.info("Cleared Telegram webhook before starting polling.")
        except BadRequest as e:
            logger.debug("Webhook cleanup skipped before polling: %s", e)
        except Exception as e:
            logger.warning("Failed to clear webhook before polling: %s", e)

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

def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.post_init = post_init
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("config", config_command))
    application.add_handler(CommandHandler("clean_joins", clean_joins_command))
    application.add_handler(CommandHandler("purge", purge_command))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_chat_members))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & (filters.ChatType.PRIVATE | filters.ChatType.GROUPS), handle_message))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    return application


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN in environment.")
    if not GROQ_API_KEY:
        raise RuntimeError("Missing GROQ_API_KEY in environment.")

    load_config()

    webhook_base_url = get_webhook_base_url()
    application = build_application()

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
    startup_attempt = 1
    while True:
        try:
            application.run_polling(drop_pending_updates=True)
            return
        except Conflict:
            logger.warning(
                "Telegram polling conflict during startup attempt %s. Retrying in %ss.",
                startup_attempt,
                POLLING_RECONNECT_DELAY,
            )
            time.sleep(POLLING_RECONNECT_DELAY)
            startup_attempt += 1
            application = build_application()

if __name__ == "__main__":
    main()
