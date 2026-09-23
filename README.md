# Telegram Chat Bot

A Telegram chat bot backed by a local llama.cpp (Ministral) server that responds only to allowed users in private (direct) messages.

## Features

- Responds only to private (direct) messages -- group chat messages are ignored
- Allowlist-based user filtering via TELEGRAM_ALLOWED_USER_IDS
- Approval-based registration requests via /start, persisted across restarts
- Conversational chat backed by a local llama.cpp / OpenAI-compatible API
- Context retention with configurable message history window
- Bounded model responses (max_tokens, character truncation)
- System prompt support: global and per-user private prompts
- Rate limiting between model requests
- Health status and conversation history commands
- Response chunking: long model outputs (> 4096 chars) are automatically split into Telegram-compatible chunks

## Requirements

- Python 3.10+
- curl (for pip bootstrap on systems without ensurepip)
- A running llama.cpp OpenAI-compatible server serving a Ministral model
  - Default model: ministral-3-3b-64k-q4_k_m.gguf
  - Default URL: http://127.0.0.1:11438/v1

## Installation

1. Clone the repository:
   git clone https://github.com/andyfied-Thargoid/telegram-chat-bot.git
   cd telegram-chat-bot

2. Run the install script (handles venv creation, pip bootstrap, and dependency installation):
   ./install.sh

   Or do it manually:
   python3 -m venv --without-pip .venv
   curl -sS --fail -o /tmp/get-pip.XXXXXX.py https://bootstrap.pypa.io/get-pip.py
   .venv/bin/python3 /tmp/get-pip.XXXXXX.py
   rm /tmp/get-pip.XXXXXX.py
   .venv/bin/python3 -m pip install -r requirements.lock

## Environment Variables

Set the following environment variables (documented in .env.example):

- TELEGRAM_TOKEN -- Bot token from @BotFather on Telegram (required).
- TELEGRAM_ALLOWED_USER_IDS -- Comma-separated Telegram numeric user IDs. If unset or empty, all Telegram users are rejected.
- TELEGRAM_ADMIN_USER_IDS -- Comma-separated Telegram numeric user IDs with authority to change the global prompt and manage registrations. If unset or empty, admin commands are denied to all users.
- TELEGRAM_REGISTRATION_FILE -- JSON registration store. Default: ~/.local/state/telegram-chat-bot/registrations.json.
- LLAMA_CPP_BASE_URL -- Base URL of the llama.cpp / OAI-compatible API. Default: http://127.0.0.1:11438/v1.
- LLAMA_CPP_MODEL -- Model name. Default: ministral-3-3b-64k-q4_k_m.gguf.
- LLAMA_CPP_TIMEOUT -- HTTP timeout in seconds. Default: 120.
- MAX_CONTEXT_MESSAGES -- Max conversation history messages per user. Default: 20.
- MAX_MESSAGE_CHARS -- Max characters per message and max model response length. Default: 8000. Response chunks are capped at 4096 characters (Telegram API limit).
- MODEL_REQUEST_INTERVAL -- Minimum seconds between requests per user. Default: 1.0.

## Numeric Setting Validation

All numeric environment variables are validated at import time:

- **LLAMA_CPP_TIMEOUT** -- must be a positive number (default: 120).
- **MAX_CONTEXT_MESSAGES** -- must be a positive integer (default: 20).
- **MAX_MESSAGE_CHARS** -- must be a positive integer (default: 8000).
- **MODEL_REQUEST_INTERVAL** -- must be a positive number (default: 1.0).

Invalid values (non-numeric, zero, or negative) raise `ValueError` before the bot starts.

## Testing

Run the full suite with:

```
pytest tests/
```

Tests cover Settings defaults, custom values, validation error paths for every numeric setting,
and core bot logic (health checks, completion, chunking, user filtering).

3. Set up your Telegram bot token:
   - Create a new bot with @BotFather on Telegram
   - Copy the token and set it as an environment variable:
    ```
cp .env.example .env
    ```

## Usage

### Direct Start

source .venv/bin/activate
python3 telegram_chat_bot.py

Or use the convenience script:

./telegram-chat-bot.sh

### Commands (private chat only)

- /start -- Approved users receive a greeting; other users create or check an access request. Optional /start parameters are rejected and never affect registration.
- /approve <telegram_user_id> -- Administrator-only approval of a pending user.
- /reject <telegram_user_id> -- Administrator-only rejection of a user.
- /revoke <telegram_user_id> -- Administrator-only revocation of approved access.
- /users -- Administrator-only list of pending and approved registrations.
- /status -- Shows current model name and history window size.
- /reset -- Clears your conversation context.
- /history -- Reports how many messages are retained in memory.
- /addglobalprompt <text> -- Sets a global system prompt for all users.
- /addprivateprompt <text> -- Sets a per-user system prompt.
- /help -- Lists all available commands.
- /shutdown -- Refused remotely; stop the local systemd service instead.

### Shutdown

Shutdown via Telegram is intentionally disabled. Stop the bot by terminating the local service or process.

## Security

- Allowlisting: TELEGRAM_ALLOWED_USER_IDS remains a static allowlist. Users approved through the persistent registration workflow are also allowed; unregistered users cannot chat or change prompts.
- Private-only: All handlers use filters.ChatType.PRIVATE; group messages are silently ignored.
- Remote shutdown disabled: The /shutdown command returns a polite refusal and suggests stopping the systemd service.
- Admin authorization: TELEGRAM_ADMIN_USER_IDS authorizes prompt and registration-management commands. If it is unset or empty, no user can change the global prompt or registrations.
- Registration: /start records a pending request only; an administrator must approve it. Registration data contains Telegram numeric IDs and should be protected and backed up as private state.

## Real Commands in Code

The bot registers the following handlers:

- /start -> start -- Greeting or registration request
- /approve -> approve -- Approve a registration
- /reject -> reject -- Reject a registration
- /revoke -> revoke -- Revoke approved access
- /users -> users -- List registration state
- /status -> status -- Model + history window
- /reset -> reset -- Clear conversation history
- /history -> history -- Report memory size
- /usage -> usage -- Report today's request and token usage
- /addglobalprompt -> addglobalprompt -- Set global system prompt
- /addprivateprompt -> addprivateprompt -- Set per-user private prompt
- /shutdown -> shutdown -- Refused remotely
- /help -> help_command -- List commands
- text (non-command) -> handle_message -- Chat completion via llama.cpp

## Usage accounting and quotas

The bot stores daily request and token counters in the same atomically-written
state file as conversation state. Counters are keyed by Telegram user and
provider. An outbound request consumes one request quota even when the provider
fails; prompt and completion token counters are updated from successful
provider responses that include usage data. A limit of `0` means unlimited.

Set `DEFAULT_DAILY_REQUEST_LIMIT` for the default provider and use the JSON
`PROVIDER_DAILY_LIMITS` map for provider-specific overrides. If
`OPENROUTER_ENABLED=true`, OpenRouter defaults to its documented 50-request
daily account limit, configurable with `OPENROUTER_DAILY_REQUEST_LIMIT`.
OpenRouter's account counter is shared across Telegram users while the
per-user counters remain available for reporting. The `/usage` command reports
the current provider's counters for the requesting user.

## Chunking

Long model responses (> 4096 characters) are automatically split into multiple Telegram messages by the internal ``_chunk`` helper, preserving the full output.

## Tests

Tests use pytest and mock the HTTP layer. Run from the project root:

.venv/bin/python -m pytest tests/test_bot.py -v

Test coverage includes:
- Private filter creation
- Settings defaults and custom environment variables
- State initialization and allowlisting
- Message assembly with global + private prompts
- Successful, empty, HTTP error, and JSON decode error paths in complete()
- Response chunking boundary conditions and full-response preservation
- handle_message rejecting an unallowlisted user
- handle_message ignoring a non-private chat
- handle_message rate limiting (wait reply; no history written)
- handle_message model failure rollback (history removed; error reply sent)
- health_check healthy and unhealthy /health endpoint scenarios
- health_check URL derivation strips /v1 suffix and trailing slashes

## systemd Deployment

The service unit telegram-chat-bot.service.example can be deployed as a user service:

1. Copy the unit file:
   cp telegram-chat-bot.service.example ~/.config/systemd/user/telegram-chat-bot.service

2. Create an environment file (never commit or push this file):
   mkdir -p ~/.config/telegram-chat-bot
   touch ~/.config/telegram-chat-bot/env
   chmod 600 ~/.config/telegram-chat-bot/env
   Add variables (at minimum TELEGRAM_TOKEN) to ~/.config/telegram-chat-bot/env.

3. Enable and start:
   systemctl --user enable --now telegram-chat-bot

The unit uses systemd StateDirectory to create the registration-state directory before startup and runs as a simple service with hardened settings (PrivateTmp, ProtectHome=read-only, ProtectSystem=strict) and restarts on failure after 5 seconds.

## Development

source .venv/bin/activate
python -m pytest tests/test_bot.py -v

## License

MIT
