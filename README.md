# Telegram Chat Bot

A Telegram chat bot backed by a local llama.cpp (Ministral) server that responds to allowed users in private messages and approved group chats.

## Features

- **Private messages**: Responds only to private (direct) messages from allowed users
- **Group chat support**: Optional group chat support with `TELEGRAM_ALLOWED_GROUP_IDS`
- Allowlist-based user filtering via TELEGRAM_ALLOWED_USER_IDS
- Group access modes: `all` (any user), `approved_users` (registered users only), or `admins` (admin-only)
- Approval-based registration requests via /start (users) and /startgroup (groups)
- Conversational chat backed by a local llama.cpp / OpenAI-compatible API
- Context retention with configurable message history window
- Bounded model responses (max_tokens, character truncation)
- System prompt support: global, per-user private prompts, and per-group prompts
- Rate limiting between model requests (per-user cooldown)
- **Concurrent request limiting** via MAX_CONCURRENT_REQUESTS setting
- **Central logging** to rotating file at `~/.local/state/telegram-chat-bot/logs/telegram-chat-bot.log` (override with `TELEGRAM_BOT_LOG_DIR=/mnt/scratch/hermes/logs` on compute01)
- **Metrics tracking** via /metrics command (request stats, success rate, avg response time)
- **Rate-limit visibility** via /ratelimit command
- **Service health check** via /health command (checks llama.cpp connectivity and reports model name)
- Response chunking: long model outputs (> 4096 chars) are automatically split into Telegram-compatible chunks
- Improved error messages: differentiated messages for timeouts, HTTP errors, and failures
- Telegram native command menu for discoverability

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
- MAX_CONCURRENT_REQUESTS -- Maximum concurrent model requests allowed. Default: 2. Set to 1 for strict serialization.
- TELEGRAM_BOT_LOG_DIR -- Log directory for rotating file handler. Default: ~/.local/state/telegram-chat-bot/logs (override with /mnt/scratch/hermes/logs on compute01).
- TELEGRAM_ALLOWED_GROUP_IDS -- Optional: Comma-separated group chat IDs to allow bot responses in (e.g., `-1001234567890`). Group messages require @botname mention in supergroups.
- GROUP_ACCESS_MODE -- Optional: Group access policy (`all`, `approved_users`, or `admins`). Default: `all`.
- USER_RATE_LIMITS -- Optional: JSON object for per-user rate limit overrides, e.g., `{"123456789": 30.0, "987654321": 60.0}` for user-specific cooldowns.

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

### Commands (private chat)

- /start -- Approved users receive a greeting; other users create or check an access request. Optional /start parameters are rejected and never affect registration.
- /approve <telegram_user_id> -- Administrator-only approval of a pending user.
- /reject <telegram_user_id> -- Administrator-only rejection of a user.
- /revoke <telegram_user_id> -- Administrator-only revocation of approved access.
- /users -- Administrator-only list of pending and approved registrations.
- /status -- Shows current model name and history window size.
- /reset -- Clears your conversation context (also works in groups).
- /history -- Reports how many messages are retained in memory (also works in groups).
- /addglobalprompt <text> -- Sets a global system prompt for all users.
- /addprivateprompt <text> -- Sets a per-user system prompt.
- /help -- Lists all available commands.
- /shutdown -- Refused remotely; stop the local systemd service instead.
- /metrics -- Shows request statistics: total/successful/failed requests, success rate, average response time, concurrent limit status.
- /ratelimit -- Shows per-user and global rate-limit status: requests in last minute, cooldown, last request age, available semaphore slots.
- /health -- Checks llama.cpp service health and reports model name.
- /usage -- Shows daily request and token usage.

### Group Chat Commands (private or in-group)

- /startgroup <group_id> -- Request to add bot to a group (admin only, private chat).
- /approvegroup <group_id> -- Approve group access (admin only).
- /rejectgroup <group_id> -- Reject group access (admin only).
- /revokegroup <group_id> -- Revoke group access (admin only).
- /groupusers -- List all approved groups (admin only).
- /setgroupprompt <group_id> <text> -- Set group-specific system prompt (admin only).
- /showgroupprompt <group_id> -- View group prompt (admin only).
- /cleargroupprompt <group_id> -- Clear group prompt (admin only).

### Usage in Groups

- In supergroups, messages must include @botname mention to trigger a response.
- Group access modes: `all` (any user), `approved_users` (registered users only), or `admins` (admin-only).
- Per-group prompts and conversation history are isolated from private chats.

### Shutdown

Shutdown via Telegram is intentionally disabled. Stop the bot by terminating the local service or process.

## Security

- **Allowlisting**: TELEGRAM_ALLOWED_USER_IDS remains a static allowlist. Users approved through the persistent registration workflow are also allowed; unregistered users cannot chat or change prompts.
- **Group access modes**: `all` (any user), `approved_users` (registered users only), or `admins` (admin-only). Configure with `GROUP_ACCESS_MODE`.
- **Remote shutdown disabled**: The /shutdown command returns a polite refusal and suggests stopping the systemd service.
- **Admin authorization**: TELEGRAM_ADMIN_USER_IDS required for prompt and registration-management commands. If it is unset or empty, no user can change the global prompt or registrations.
- **Registration**: /start records a pending request only; an administrator must approve it. Registration data contains Telegram numeric IDs and should be protected and backed up as private state.
- **Private prompt isolation**: Private user prompts are never leaked into group chat requests.

## Real Commands in Code

The bot registers the following handlers:

### Private-Only Commands
- /start -> start -- Greeting or registration request
- /approve -> approve -- Approve a registration
- /reject -> reject -- Reject a registration
- /revoke -> revoke -- Revoke approved access
- /users -> users -- List registration state
- /status -> status -- Model + history window
- /metrics -> metrics -- Request statistics
- /ratelimit -> ratelimit -- Rate-limit status
- /health -> health -- Service health check
- /usage -> usage -- Daily usage report
- /addglobalprompt -> addglobalprompt -- Set global system prompt
- /addprivateprompt -> addprivateprompt -- Set per-user system prompt
- /shutdown -> shutdown -- Refused remotely
- /help -> help_command -- List commands
- /resilience -> resilience_status -- Circuit breaker status

### Group Commands
- /startgroup -> startgroup -- Request to add bot to group
- /approvegroup -> approvegroup -- Approve group access
- /rejectgroup -> rejectgroup -- Reject group access
- /revokegroup -> revokegroup -- Revoke group access
- /groupusers -> groupusers -- List approved groups
- /setgroupprompt -> setgroupprompt -- Set group-specific prompt
- /showgroupprompt -> showgroupprompt -- View group prompt
- /cleargroupprompt -> cleargroupprompt -- Clear group prompt

### Universal Commands (private and group)
- /reset -> reset -- Clear conversation history
- /history -> history -- Report memory size
- text (non-command) -> handle_message -- Chat completion via llama.cpp

### Usage accounting and quotas

Per-user rate limits:
Set `USER_RATE_LIMITS` in your `.env` file to override the global `MODEL_REQUEST_INTERVAL` for specific users. Format: JSON object mapping Telegram user IDs to cooldown seconds (in seconds). Example:
```env
USER_RATE_LIMITS={"123456789": 30.0, "987654321": 60.0}
```
This is useful for throttling power users while allowing others faster response times.

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

## Concurrent Request Limiting

To protect the llama.cpp server from overload, the bot limits concurrent model requests using `MAX_CONCURRENT_REQUESTS` (default: 2). When multiple users send messages simultaneously, requests are queued up to the configured limit. Use `/ratelimit` to see current semaphore state and available slots.

## Central Logging

All bot activity is logged to `~/.local/state/telegram-chat-bot/logs/telegram-chat-bot.log` with rotating file handler (10MB per file, 5 backups). Logs include request timestamps, errors, and provider failures. Override with `TELEGRAM_BOT_LOG_DIR=/mnt/scratch/hermes/logs` on compute01.

## Group Chat Support

To enable group chat support:

1. Set `TELEGRAM_ALLOWED_GROUP_IDS` in your `.env` file with comma-separated group IDs (e.g., `-1001234567890`). Group message IDs are negative.
2. Optionally set `GROUP_ACCESS_MODE`:
   - `all` (default): Any user in allowed groups can interact
   - `approved_users`: Only users who have been approved via `/start` can interact
   - `admins`: Only users in `TELEGRAM_ADMIN_USER_IDS` can interact

3. In supergroups, messages must include @botname mention to trigger a response.

### Group Commands

Admin commands for group management:
- `/startgroup <group_id>` - Request to add bot to a group (private chat only)
- `/approvegroup <group_id>` - Approve group access
- `/rejectgroup <group_id>` - Reject group access
- `/revokegroup <group_id>` - Revoke group access
- `/groupusers` - List all approved groups

### Group Prompts

Admin commands for group-specific prompts:
- `/setgroupprompt <group_id> <prompt>` - Set group-specific system prompt
- `/showgroupprompt <group_id>` - View current group prompt
- `/cleargroupprompt <group_id>` - Remove group prompt

Group prompts are isolated from private user prompts and only apply to group conversations.

---

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

The unit uses systemd StateDirectory to create the persistent state directory before startup. Both `TELEGRAM_REGISTRATION_FILE` and `TELEGRAM_CHAT_BOT_STATE_FILE` point into `%S/telegram-chat-bot`, which remains writable despite `ProtectHome=read-only` and `ProtectSystem=strict`. The service runs as a simple service and restarts on failure after 5 seconds.

The default state path follows the XDG user-state convention at `~/.local/state/telegram-chat-bot/state.json`. The legacy `~/.telegram-chat-bot/state.json` path is read only during migration and is never used for new writes.

## Development

source .venv/bin/activate
python -m pytest tests/test_bot.py -v

## License

MIT
## Fixes Summary

This PR fixes three critical issues:

1. History consistency race under concurrent requests
2. Circuit breaker failure count never resetting  
3. Circuit breaker thread safety

See commit 522348e for details.
