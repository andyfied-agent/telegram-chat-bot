#!/usr/bin/env bash
set -euo pipefail

# Install script for Telegram Chat Bot

echo "Installing Telegram Chat Bot..."

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 is required but not installed."
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Creating isolated virtual environment..."
python3 -m venv --without-pip .venv

# Bootstrap pip (ensurepip may not be available on minimal systems)
if .venv/bin/python3 -m pip --version &> /dev/null; then
    echo "pip already available."
else
    echo "Bootstrapping pip..."
    if ! command -v curl &> /dev/null; then
        echo "ERROR: curl is required to bootstrap pip."
        exit 1
    fi
    curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    .venv/bin/python3 /tmp/get-pip.py
    rm /tmp/get-pip.py
    echo "pip installed."
fi

echo "Upgrading pip..."
.venv/bin/python3 -m pip install --upgrade pip

echo "Installing dependencies..."
.venv/bin/python3 -m pip install -r requirements.lock

echo ""
echo "Installation complete!"
echo "To use the bot:"
echo "  1. Get a bot token from @BotFather on Telegram"
echo "  2. Set the token as an environment variable:"
echo "     export TELEGRAM_TOKEN="your_token_here""
echo "  3. Run the bot with:"
echo "     ./telegram-chat-bot.sh"
