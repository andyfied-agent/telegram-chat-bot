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

# Install required dependencies
echo "Installing dependencies..."
pip3 install python-telegram-bot

echo "Installation complete!"
echo "To use the bot:"
echo "1. Get a bot token from @BotFather on Telegram"
echo "2. Set the token as an environment variable:"
echo "   export TELEGRAM_TOKEN=\"your_token_here\""
echo "3. Run the bot with:"
echo "   ./telegram-chat-bot.sh"