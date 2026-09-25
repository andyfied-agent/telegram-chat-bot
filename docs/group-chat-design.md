# Group Chat Support Design

## Overview
Extended Telegram bot with full group chat support, including per-group state, group-scoped prompts, and configurable access policies.

## Environment Variables

### Group Configuration
- `TELEGRAM_ALLOWED_GROUP_IDS` - Comma-separated group chat IDs (optional, empty = group chat disabled)
- `GROUP_ACCESS_MODE` - Group access policy: `all` (any user), `approved_users` (must be registered), or `admins` (admin-only). Default: `all`
- `TELEGRAM_ADMIN_USER_IDS` - Comma-separated Telegram user IDs that can manage group permissions

### Logging
- `TELEGRAM_BOT_LOG_DIR` - Log directory. Default: `~/.local/state/telegram-chat-bot/logs` (override with `/mnt/scratch/hermes/logs` on compute01)

## Features

### 1. Group Message Filtering
- Only process messages from allowed groups (static allowlist OR approved registration)
- Supports both `TELEGRAM_ALLOWED_GROUP_IDS` and group registration workflow
- Silent ignore for disallowed groups

### 2. Per-Group State
- Conversation history stored per `(user_id, group_id)` tuple
- Private history: `user_id` only (group_id=None)
- Group history: `(user_id, group_id)` tuple
- State persistence in JSON files

### 3. Group-Scoped Prompts
- Separate prompt storage: `global_prompt`, `private_prompts[user_id]`, `group_prompts[group_id]`
- Group requests use `global_prompt + group_prompt[group_id]` (never private prompt)
- Private requests use `global_prompt + private_prompt[user_id]`

### 4. Group Registration System
- `/startgroup <group_id>` - Admin requests to add bot to group (private)
- `/approvegroup <group_id>` - Admin approves group access
- `/rejectgroup <group_id>` - Admin rejects group access
- `/revokegroup <group_id>` - Admin removes group access
- `/groupusers` - List approved groups (admin only)

### 5. Group Prompt Management (Admin Only)
- `/setgroupprompt <group_id> <prompt>` - Set prompt for specific group (private or in-group)
- `/showgroupprompt <group_id>` - View group prompt
- `/cleargroupprompt <group_id>` - Clear group prompt

### 6. Group Context Commands
- `/reset` - Reset conversation context (works in both private and group chats)
- `/history` - Show message count (works in both private and group chats)
- Per-user group history isolation

### 7. Configurable Group Access Policy
- `GROUP_ACCESS_MODE=all` - Any user can use approved groups (default)
- `GROUP_ACCESS_MODE=approved_users` - Only registered users can use groups
- `GROUP_ACCESS_MODE=admins` - Only bot administrators can use groups
- Validation: Invalid modes raise `ValueError` at startup

## Architecture

### Data Model
```python
class State:
    # History: user_id -> deque for private, (user_id, group_id) -> deque for groups
    history: dict[int | tuple[int, int], deque[dict[str, str]]]
    
    # Prompts
    global_prompt: str
    private_prompts: dict[int, str]        # user_id -> prompt
    group_prompts: dict[int, str]          # group_id -> prompt
    
    # Registration stores
    user_registrations: RegistrationStore
    group_registrations: RegistrationStore  # Separate store for groups
```

### State Key Format
- Private: `user_id` (int)
- Group: `(user_id, group_id)` (tuple[int, int])
- Serialization: `user_key_str` = `user_id` or `"user_id:group_id"`

### Prompt Selection Logic
```python
def messages(self, user_id: int, group_id: int | None = None) -> list[dict]:
    prompt = self.global_prompt
    if group_id is not None:
        # Group request: use group prompt, never private prompt
        if self.group_prompts.get(group_id):
            prompt = f"{prompt}\n{self.group_prompts[group_id]}".strip()
    else:
        # Private request: use private prompt
        if self.private_prompts.get(user_id):
            prompt = f"{prompt}\n{self.private_prompts[user_id]}".strip()
    return [{"role": "system", "content": prompt}] + self.history[key]
```

### Group Authorization
```python
def allowed_group(self, group_id: int, user_id: int | None = None) -> bool:
    mode = self.settings.group_access_mode
    
    if group_id in self.settings.allowed_group_ids or \
       self.group_registrations.status(group_id) == "approved":
        if mode == "all":
            return True  # Any user allowed
        elif mode == "approved_users":
            return self.allowed(user_id) if user_id else False
        elif mode == "admins":
            return self.admin(user_id) if user_id else False
        return user_id is not None  # Fail closed for restricted modes
    
    return False
```

## Command Registration

### Private-Only Commands (filtered)
- `/start`, `/approve`, `/reject`, `/revoke`, `/users` - Registration management
- `/status`, `/metrics`, `/ratelimit`, `/usage`, `/resilience` - System info
- `/addglobalprompt`, `/addprivateprompt` - Prompt management
- `/shutdown`, `/help` - Utilities

### Universal Commands (no filter)
- `/reset` - Clear context (private or group)
- `/history` - Show history count (private or group)

### Group Prompt Commands (no filter)
- `/setgroupprompt` - Works in private with `<group_id> <prompt>` or in-group with `<prompt>`
- `/showgroupprompt <group_id>` - Always requires group_id
- `/cleargroupprompt <group_id>` - Always requires group_id

## Implementation Checklist

### Phase 1: Core Support ✅
- [x] Update `State` class to handle `(user_id, group_id)` tuples
- [x] Add `TELEGRAM_ALLOWED_GROUP_IDS` validation
- [x] Modify `handle_message` to check group allowlist
- [x] Add @mention botname detection for groups

### Phase 2: Registration System ✅
- [x] Extend `RegistrationStore` to handle group IDs
- [x] Add `/startgroup` handler
- [x] Add `/approvegroup`, `/rejectgroup`, `/revokegroup` handlers
- [x] Update `/users` to show group registrations (separate from users)
- [x] Add `/groupusers` command to list group registrations

### Phase 3: Group Context & Prompts ✅
- [x] Extend `/reset` to work in groups with per-user context
- [x] Extend `/history` to work in groups with per-user context
- [x] Add `group_prompts` dict to State
- [x] Add `/setgroupprompt`, `/showgroupprompt`, `/cleargroupprompt` commands
- [x] Update `messages()` to use group prompts for group requests
- [x] Ensure private prompts never leak into group requests

### Phase 4: Access Policy & Polish ✅
- [x] Add `GROUP_ACCESS_MODE` setting with validation
- [x] Update `allowed_group()` to enforce access modes
- [x] Register Telegram native command menu for all scopes
- [x] Improve user-facing error messages (timeouts, HTTP errors, failures)
- [x] Add test coverage for all group features

## Test Coverage
- `tests/test_issue33_prompts.py` - Private prompts isolation tests
- `tests/test_issue38_group_reset_history.py` - Group command reachability tests
- `tests/test_issue34_group_access_mode.py` - GROUP_ACCESS_MODE validation tests

## Security Considerations
- Group chats are inherently less secure than private messages
- No direct messages to the bot in groups (must use @mention)
- Admin commands require explicit `TELEGRAM_ADMIN_USER_IDS` validation
- Group prompts only affect group requests, never private chats
- Access modes fail closed: `approved_users`/`admins` modes require `user_id` parameter

## Related Files
- `telegram_chat_bot.py` - Main bot logic with group support
- `tests/test_bot.py` - Existing bot tests
- `tests/test_issue33_prompts.py` - Private prompts isolation tests
- `tests/test_issue38_group_reset_history.py` - Group command tests
- `tests/test_issue34_group_access_mode.py` - Access mode tests
- `ISSUES.md` - Issue tracking and status
