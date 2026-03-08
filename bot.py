import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PROJECT_DOCS = os.getenv("PROJECT_DOCS", "")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction=f"""You are a helpful assistant for Animal AI, a crypto project focused on DeFi and animal charity.
Use this documentation: {PROJECT_DOCS}

Be concise and helpful."""
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🐾 Welcome to Animal AI!\n\n"
        "I'm your dedicated guide to the Animal AI ecosystem where every trade helps a paw. 🐕\n\n"
        "Ask me anything about our mission, features, donations, and more!\n\n"
        "Small trades, big barks, and even bigger hearts. ❤️"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Ask me questions about Animal AI!\n\n"
        "/start - Welcome\n"
        "/help - This message"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_message = update.message.text
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        
        response = model.generate_content(user_message)
        answer = response.text
        await update.message.reply_text(answer)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("Sorry, error occurred. Try again.")

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
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.post_init = post_init
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("Starting Animal AI Bot...")
    application.run_polling()

if __name__ == "__main__":
    main()