#!/bin/bash

# Start script for Telegram Chat Bot

echo "Starting Telegram Chat Bot..."

# Check if token is set
if [ -z "$TELEGRAM_TOKEN" ]; then
    echo "TELEGRAM_TOKEN environment variable is not set."
    echo "Please set it before running the bot:"
    echo "export TELEGRAM_TOKEN=\"your_token_here\""
    exit 1
fi

# Run the bot
python3 telegram-chat-bot.py