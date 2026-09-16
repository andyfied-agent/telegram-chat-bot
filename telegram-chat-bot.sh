#!/usr/bin/env bash
set -euo pipefail

# Start script for Telegram Chat Bot

echo "Starting Telegram Chat Bot..."

# Check if token is set
if [ -z "${TELEGRAM_TOKEN:-}" ]; then
    echo "ERROR: TELEGRAM_TOKEN environment variable is not set."
    echo "Please set it before running the bot:"
    echo '  export TELEGRAM_TOKEN="your_token_here"'
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Verify .venv exists and has python
if [ ! -f "$SCRIPT_DIR/.venv/bin/python3" ]; then
    echo "ERROR: .venv/bin/python3 not found. Run install.sh first."
    exit 1
fi

exec "$SCRIPT_DIR/.venv/bin/python3" "$SCRIPT_DIR/telegram_chat_bot.py"
