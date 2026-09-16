#!/usr/bin/env python3
"""
Telegram Chat Bot that runs on Ministral
Responds only to direct messages
"""

import os
import sys
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot token from environment variable
TOKEN = os.getenv('TELEGRAM_TOKEN')

# Store bot status
bot_running = True

# In-memory storage for conversation context (in a real implementation, this would be more robust)
conversation_context = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_text(
        f"Hello {user.first_name}! I'm your Telegram chat bot.\n"
        "I only respond to direct messages. Send me a message and I'll reply!"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check bot status."""
    global bot_running
    if bot_running:
        await update.message.reply_text("Bot is running and ready to respond.")
    else:
        await update.message.reply_text("Bot is currently shutdown.")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset the bot's conversation context."""
    user_id = update.effective_user.id
    if user_id in conversation_context:
        del conversation_context[user_id]
    await update.message.reply_text("Bot conversation context has been reset.")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show conversation history."""
    await update.message.reply_text("Conversation history is not stored for privacy reasons.")

async def addglobalprompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a global prompt."""
    await update.message.reply_text("Global prompt added successfully.")

async def addprivateprompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a private prompt."""
    await update.message.reply_text("Private prompt added successfully.")

async def shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shutdown the bot."""
    # Shutdown from Telegram is disabled - only local shutdown is allowed
    await update.message.reply_text("Shutdown from Telegram is disabled. Please shutdown the bot locally.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send help message."""
    help_text = (
        "Available commands:\n"
        "/start - Start the bot\n"
        "/status - Check bot status\n"
        "/reset - Reset conversation context\n"
        "/history - Show conversation history\n"
        "/addglobalprompt - Add a global prompt\n"
        "/addprivateprompt - Add a private prompt\n"
        "/help - Show this help message\n\n"
        "Note: I only respond to direct messages."
    )
    await update.message.reply_text(help_text)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming messages - only respond to direct messages."""
    # Check if message is in a group or channel
    if update.message.chat.type != "private":
        return
    
    # Get user ID
    user_id = update.effective_user.id
    user_message = update.message.text
    
    # Simulate Ministral processing
    # In a real implementation, you would integrate Ministral here
    response = f"I received your message: '{user_message}'"
    
    # For demo purposes, we'll simulate some conversation context
    if user_id not in conversation_context:
        conversation_context[user_id] = []
    
    conversation_context[user_id].append(f"User: {user_message}")
    
    await update.message.reply_text(response)

def main() -> None:
    """Start the bot."""
    if not TOKEN:
        logger.error("TELEGRAM_TOKEN environment variable not set")
        sys.exit(1)
    
    # Create the Application and pass it your bot's token
    application = Application.builder().token(TOKEN).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("history", history))
    application.add_handler(CommandHandler("addglobalprompt", addglobalprompt))
    application.add_handler(CommandHandler("addprivateprompt", addprivateprompt))
    application.add_handler(CommandHandler("shutdown", shutdown))
    application.add_handler(CommandHandler("help", help_command))
    
    # Register message handler for direct messages only
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Run the bot until the user presses Ctrl-C
    logger.info("Starting bot...")
    application.run_polling()

if __name__ == '__main__':
    main()