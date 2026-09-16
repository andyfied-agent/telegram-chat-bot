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

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/telegram-chat-bot.py"
