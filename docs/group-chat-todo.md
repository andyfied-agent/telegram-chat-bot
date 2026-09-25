# Group Chat Implementation Tracker

## Phase 1: Core Support ✅

- [x] Update `State` class to handle (user_id, group_id) tuples
- [x] Add `TELEGRAM_ALLOWED_GROUP_IDS` validation
- [x] Modify `handle_message` to check group allowlist
- [x] Add @mention botname detection (not prefix)

## Phase 2: Registration System ✅

- [x] Extend `RegistrationStore` to handle group IDs
- [x] Add `/startgroup` handler
- [x] Add `/approvegroup`, `/rejectgroup`, `/revokegroup` handlers
- [x] Update `/users` to show group registrations (separate from users)
- [x] Add `/groupusers` command to list group registrations

## Phase 3: Admin Commands (Optional)

- [ ] Add group admin command handlers (`/groupstatus`)
- [ ] Implement per-group usage statistics
- [ ] Add @mention prefix enforcement option

## Phase 4: Testing

- [ ] Mock group chat messages in tests
- [ ] Test group allowlist filtering
- [ ] Test @mention botname detection (not prefix)
- [ ] Test group registration workflow
- [ ] Test group registration with negative IDs

## Testing Checklist

### Unit Tests
- [x] `test_group_chat_allowlist_filtering`
- [x] `test_group_mention_botname_detection`
- [x] `test_state_group_isolation`
- [ ] `test_group_registration_flow`
- [ ] `test_group_admin_commands`

### Integration Tests
- [ ] Bot responds to messages mentioning @telegram-bot in group
- [ ] Bot ignores group message without @mention
- [ ] Bot ignores messages from disallowed group
- [ ] Bot processes private messages unchanged

## Current Status

**Completed:**
- Phase 1: Core infrastructure for per-group history and @mention detection
- Phase 2: Group registration system with approval workflow

**To Complete (Optional Phase 3):**
- Group status commands
- Per-group usage stats
- Configurable @mention behavior

**Documentation:**
- Updated docs/group-chat-design.md
- Updated docs/group-chat-todo.md

---
**Last updated:** After PR #26 fixes
**Status:** Ready for merge after tests pass
