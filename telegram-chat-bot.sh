#!/usr/bin/env bash
set -euo pipefail

# Start script for Telegram Chat Bot

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Load .env file if it exists
if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

# Check if token is set
if [ -z "${TELEGRAM_TOKEN:-}" ]; then
    echo "ERROR: TELEGRAM_TOKEN environment variable is not set." >&2
    echo "Please set it in .env file or as environment variable." >&2
    exit 1
fi

# Verify .venv exists and has python
if [ ! -f "$SCRIPT_DIR/.venv/bin/python3" ]; then
    echo "ERROR: .venv/bin/python3 not found. Run update.sh first." >&2
    exit 1
fi

# Run the bot
exec "$SCRIPT_DIR/.venv/bin/python3" "$SCRIPT_DIR/telegram_chat_bot.py"
