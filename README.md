# Telegram Chat Bot

A Telegram chat bot running on Ministral that responds only to direct messages. The bot is designed to be easy to install, invoke, check status, and shutdown.

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

2. Install dependencies:
   ```
   pip install python-telegram-bot
   ```

3. Set up your Telegram bot token:
   - Create a new bot with @BotFather on Telegram
   - Copy the token and set it as an environment variable:
     ```
     export TELEGRAM_TOKEN="your_token_here"
     ```

## Usage

To start the bot:
```
python bot.py
```

### Commands

- `/start` - Start the bot
- `/status` - Check bot status
- `/shutdown` - Shutdown the bot (only available when running)
- `/help` - Show this help message

## Security

The bot only responds to direct messages to ensure privacy and prevent spam in group chats.

## Requirements

- Python 3.7+
- python-telegram-bot library

## Running the Bot

The bot can be started with:
```
python bot.py
```

To check the status:
```
# In another terminal, while the bot is running
curl http://localhost:8000/status
```

To shutdown the bot:
```
# In another terminal, while the bot is running
curl http://localhost:8000/shutdown
```

## Development

For development, you can use Poetry to manage dependencies:
```
poetry install
```

## License

MIT