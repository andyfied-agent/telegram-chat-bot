# Group Chat Implementation Tracker

## Current Status: All Phases Complete ✅

### Phase 1: Core Support ✅ COMPLETE
- [x] Update `State` class to handle `(user_id, group_id)` tuples
- [x] Add `TELEGRAM_ALLOWED_GROUP_IDS` validation
- [x] Modify `handle_message` to check group allowlist
- [x] Add @mention botname detection for groups

### Phase 2: Registration System ✅ COMPLETE
- [x] Extend `RegistrationStore` to handle group IDs
- [x] Add `/startgroup` handler
- [x] Add `/approvegroup`, `/rejectgroup`, `/revokegroup` handlers
- [x] Update `/users` to show group registrations (separate from users)
- [x] Add `/groupusers` command to list group registrations

### Phase 3: Group Context & Prompts ✅ COMPLETE
- [x] Extend `/reset` to work in groups with per-user context
- [x] Extend `/history` to work in groups with per-user context
- [x] Add `group_prompts` dict to State
- [x] Add `/setgroupprompt`, `/showgroupprompt`, `/cleargroupprompt` commands
- [x] Update `messages()` to use group prompts for group requests
- [x] Ensure private prompts never leak into group requests

### Phase 4: Access Policy & Polish ✅ COMPLETE
- [x] Add `GROUP_ACCESS_MODE` setting with validation (`all`/`approved_users`/`admins`)
- [x] Update `allowed_group()` to enforce access modes (fail closed)
- [x] Register Telegram native command menu for all scopes
- [x] Improve user-facing error messages (timeouts, HTTP errors, failures)
- [x] Add test coverage for all group features

## Commands Reference

### Private Chat Commands
| Command | Description | Access |
|---------|-------------|--------|
| `/start` | Request access or check status | All users |
| `/approve <id>` | Approve a user | Admin only |
| `/reject <id>` | Reject a user | Admin only |
| `/revoke <id>` | Revoke user access | Admin only |
| `/users` | List registrations | Admin only |
| `/status` | Show model and history info | All users |
| `/metrics` | Show request statistics | All users |
| `/ratelimit` | Show rate limit status | All users |
| `/usage` | Show daily usage | All users |
| `/resilience` | Show circuit breaker status | All users |
| `/reset` | Clear conversation history | Registered users |
| `/history` | Show message count in memory | Registered users |
| `/addglobalprompt <text>` | Set global system prompt | Admin only |
| `/addprivateprompt <text>` | Set per-user private prompt | Registered users |
| `/shutdown` | Shutdown the bot (local only) | All users |
| `/help` | Show all available commands | All users |

### Group Commands (Private)
| Command | Description | Access |
|---------|-------------|--------|
| `/startgroup <id>` | Request to add bot to a group | Admin only |
| `/approvegroup <id>` | Approve a group | Admin only |
| `/rejectgroup <id>` | Reject a group | Admin only |
| `/revokegroup <id>` | Revoke group access | Admin only |
| `/groupusers` | List approved groups | Admin only |

### Group Prompt Commands (Private or Group)
| Command | Description | Access |
|---------|-------------|--------|
| `/setgroupprompt <group_id> <prompt>` | Set group-specific prompt | Admin only |
| `/showgroupprompt <group_id>` | Show group prompt | Admin only |
| `/cleargroupprompt <group_id>` | Clear group prompt | Admin only |

## Usage Examples

### Group Setup
```bash
# In private chat, admin sends:
/setgroupprompt 123456789 "You are a helpful group assistant for team discussions."

# In group chat:
/setgroupprompt "You are a helpful group assistant for team discussions."
```

### Access Control
```bash
# Default (all users can use groups):
GROUP_ACCESS_MODE=all

# Restrict to registered users only:
GROUP_ACCESS_MODE=approved_users

# Restrict to admins only:
GROUP_ACCESS_MODE=admins
```

### Portable Logging
```bash
# Default portable location:
# ~/.local/state/telegram-chat-bot/logs/telegram-chat-bot.log

# Override for compute01:
export TELEGRAM_BOT_LOG_DIR="/mnt/scratch/hermes/logs"
```

## Test Files
- `tests/test_issue33_prompts.py` - Private prompts isolation
- `tests/test_issue38_group_reset_history.py` - Group command reachability
- `tests/test_issue34_group_access_mode.py` - Access mode validation

## Known Limitations
- Group chats are inherently less secure than private messages
- No @all/@everyone mention handling (security consideration)
- No per-group rate limits from separate pool (uses shared semaphore)
- Group prompt commands require admin authorization

## Future Enhancements (Out of Scope)
- Per-group usage statistics
- Shared group history mode (all users share same history)
- @all/@everyone mention handling
- Separate rate limit pools per group
- Chat Shift integration (blocked)

---

**Last updated:** 2026-09-25  
**Status:** All tracked issues complete, ready for release  
**Blocked issues:** #8, #9, #19, #20 (pending Chat Shift offline)
