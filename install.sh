#!/bin/bash

# Install script for Telegram Chat Bot

echo "Installing Telegram Chat Bot..."

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "Python3 is required but not installed."
    exit 1
fi

# Check if pip is installed
if ! command -v pip3 &> /dev/null; then
    echo "pip3 is required but not installed."
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
echo "Creating isolated virtual environment..."
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock

echo "Installation complete!"
echo "To use the bot:"
echo "1. Get a bot token from @BotFather on Telegram"
echo "2. Set the token as an environment variable:"
echo "   export TELEGRAM_TOKEN=\"your_token_here\""
echo "3. Run the bot with:"
echo "   ./telegram-chat-bot.sh"
