#!/usr/bin/env bash
set -euo pipefail

# Production update script for Telegram Chat Bot
# Safely pulls latest code and updates dependencies without overwriting .env

echo "Telegram Chat Bot - Update Script"
echo "=================================="

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check if this is a git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo "ERROR: Not a git repository"
    exit 1
fi

# Check if .env exists (user secrets)
if [ -f .env ]; then
    echo "✓ Existing .env file detected (preserving secrets)"
    ENV_EXISTS="yes"
else
    echo "⚠ No .env file found"
    ENV_EXISTS="no"
fi

# Check if already installed (has .venv)
if [ -d .venv ]; then
    echo "✓ Virtual environment already exists"
    ALREADY_INSTALLED="yes"
else
    echo "Creating virtual environment..."
    python3 -m venv .venv
    ALREADY_INSTALLED="no"
fi

# Pull latest changes from main branch
echo "Pulling latest changes from origin/main..."
git fetch origin
git pull origin main

# Update dependencies only if .venv exists
if [ "$ALREADY_INSTALLED" = "yes" ]; then
    echo "Updating dependencies..."
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.lock
else
    echo "Installing dependencies..."
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.lock
fi

# Handle .env file
if [ "$ENV_EXISTS" = "no" ] && [ -f .env.example ]; then
    echo "Creating new .env file from .env.example..."
    cp .env.example .env
    echo ""
    echo "⚠ IMPORTANT: You need to set your TELEGRAM_TOKEN in .env file:"
    echo "   Edit .env and add: TELEGRAM_TOKEN=\"your_token_here\""
    echo ""
fi

# Create log directory if it doesn't exist
log_dir="${TELEGRAM_BOT_LOG_DIR:-~/.local/state/telegram-chat-bot/logs}"
log_dir_expanded="${log_dir/#\~/$HOME}"
mkdir -p "$log_dir_expanded"

echo ""
echo "Update complete!"
echo "---------------"

# Restart the bot service if systemd is available and running
if command -v systemctl > /dev/null 2>&1 && systemctl --user is-active --quiet telegram-chat-bot; then
    systemctl --user restart telegram-chat-bot
    echo "✓ Bot service restarted successfully"
elif [ "$ENV_EXISTS" = "no" ]; then
    echo "⚠ Set TELEGRAM_TOKEN before starting manually"
else
    echo "ℹ Bot service not running. Start manually with: ./telegram-chat-bot.sh"
fi
echo "---------------"
