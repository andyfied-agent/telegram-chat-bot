# Telegram Chat Bot

A Telegram chat bot backed by a local Ministral llama.cpp server that responds only to direct messages.

## Features

- Responds only to direct messages (not group chats)
- Easy installation and setup
- Simple commands for status checks and shutdown
- Runs locally on your computer
- Uses Ministral for natural language processing

## Installation

1. Clone the repository:
   ```
   git clone https://github.com/andyfied-Thargoid/telegram-chat-bot.git
   ```

2. Run the install script:
   ```
   ./install.sh
   ```

3. Set up your Telegram bot token:
   - Create a new bot with @BotFather on Telegram
   - Copy the token and set it as an environment variable:
     ```
   export TELEGRAM_TOKEN="your_token_here"
   export TELEGRAM_ALLOWED_USER_IDS="123456789"
   export LLAMA_CPP_BASE_URL="http://127.0.0.1:11438/v1"
     ```

## Usage

To start the bot:
```
./telegram-chat-bot.sh
```

### Commands

- `/start` - Start the bot
- `/status` - Check bot status
- `/reset` - Reset conversation context
- `/history` - Show conversation history
- `/addglobalprompt` - Add a global prompt
- `/addprivateprompt` - Add a private prompt
- `/help` - Show this help message

## Security

The bot only responds to direct messages to ensure privacy and prevent spam in group chats. Shutdown commands can only be issued locally, not through Telegram.

## Requirements

- Python 3.7+
- python-telegram-bot library

## Running the Bot

The bot can be started with:
```
python bot.py
```

The `/status` and `/shutdown` commands are available only through private Telegram messages; shutdown is intentionally refused remotely.
Set `TELEGRAM_ALLOWED_USER_IDS` to a comma-separated list of Telegram numeric user IDs; if it is unset, all Telegram users are rejected.

## Development

For development, use the isolated environment created by `install.sh`:
```
./.venv/bin/python -m unittest discover -s tests -v
```

## License

MIT
