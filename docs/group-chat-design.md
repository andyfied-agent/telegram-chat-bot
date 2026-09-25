# Group Chat Support Design

## Overview
Extend the Telegram bot to optionally handle messages in allowed groups, with per-group permissions and @mention prefix support.

## Requirements

### Environment Variables
- `TELEGRAM_ALLOWED_GROUP_IDS` - Comma-separated group chat IDs (optional, empty = group chat disabled)
- `TELEGRAM_ADMIN_USER_IDS` - Comma-separated Telegram user IDs that can manage group permissions

### Features
1. **Group message filtering**: Only process messages from allowed groups
2. **@mention botname**: In group chats, messages must mention the bot (e.g., @telegram-bot) to trigger responses
3. **Per-group state**: Conversation history and usage counters per (user, group) pair
4. **Group-specific registration**: Groups need approval before the bot can respond in them via `/startgroup`
5. **Admin commands**: Only `TELEGRAM_ADMIN_USER_IDS` users can manage group permissions

### Security Considerations
- Group chats are inherently less secure than private messages
- No direct messages to the bot in groups (must use @mention)
- Admin commands in groups require explicit admin user ID validation

## Architecture Changes

### Data Model
```python
class State:
    # Current: user_id -> list[dict]
    # New: (user_id, group_id) -> list[dict]  # group_id = None for private
    
    def get_history(self, user_id: int, group_id: Optional[int] = None) -> List[dict]
    def append_history(self, user_id: int, message: dict, group_id: Optional[int] = None) -> None
    def clear_history(self, user_id: int, group_id: Optional[int] = None) -> None
```

### Message Handler
```python
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Determine if group or private
    group_id = update.effective_message.chat.id if update.effective_message.chat.type == Chat.GROUP else None
    
    # Check if group is allowed (skip if private)
    if group_id and group_id not in get_allowed_groups():
        return  # Silent ignore
    
    # Check @mention requirement for groups
    if group_id:
        if not has_mention_botname(update.effective_message, bot_username):
            return  # Silent ignore in groups
    else:
        # Private chat: no @mention required
        pass
    
    # Rest of handler unchanged (allows, rate limiting, etc.)
```

### Group Registration Flow
1. Admin sends `/startgroup <group_id>` privately (admin-only command)
2. Bot creates pending group registration (stored in `registrations.json` with group ID)
3. Admin approves via `/approvegroup <group_id>`
4. Bot starts responding in that group

### Commands (Group Context)
- `/startgroup <group_id>` - Admin sends this command privately to request adding bot to a group
- `/approvegroup <group_id>` - Admin approves group access
- `/rejectgroup <group_id>` - Admin rejects group access
- `/revokegroup <group_id>` - Admin removes group access
- `/groupusers` - List groups bot is registered in
- `/groupstatus <group_id>` - Show group-specific stats

## Implementation Plan

### Phase 1: Core Support
1. Update `State` class to handle (user_id, group_id) tuples
2. Add `TELEGRAM_ALLOWED_GROUP_IDS` validation
3. Modify `handle_message` to check group allowlist
4. Add @mention prefix detection for groups

### Phase 2: Registration System
1. Extend `RegistrationStore` to handle group IDs
2. Add `/startgroup` handler
3. Add `/approvegroup`, `/rejectgroup`, `/revokegroup` handlers
4. Update `/users` to show group registrations

### Phase 3: Admin Commands
1. Add group admin command handlers (`/groupusers`, `/groupstatus`)
2. Implement per-group usage statistics
3. Add @mention prefix enforcement option

### Phase 4: Testing
1. Mock group chat messages in tests
2. Test group allowlist filtering
3. Test @mention botname detection (not prefix)
4. Test group registration workflow

## Open Questions

1. Should @mention be mandatory in groups? (default: yes)
2. Should the bot respond to @all/@everyone mentions? (default: no for security)
3. How to handle group admins vs bot admin permissions?
4. Should groups have separate rate limits from private messages?

## Related Files
- `telegram_chat_bot.py` - Main bot logic
- `tests/test_bot.py` - Existing bot tests
- `tests/` - New test files for group chat functionality
