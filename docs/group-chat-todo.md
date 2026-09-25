# Group Chat Implementation Tracker

## Phase 1: Core Support ✅

- [x] Update `State` class to handle (user_id, group_id) tuples
- [x] Add `TELEGRAM_ALLOWED_GROUP_IDS` validation
- [x] Modify `handle_message` to check group allowlist
- [x] Add @mention prefix detection for groups

## Phase 2: Registration System

- [ ] Extend `RegistrationStore` to handle group IDs
- [ ] Add `/startgroup` handler
- [ ] Add `/approvegroup`, `/rejectgroup`, `/revokegroup` handlers
- [ ] Update `/users` to show group registrations

## Phase 3: Admin Commands

- [ ] Add group admin command handlers (`/groupusers`, `/groupstatus`)
- [ ] Implement per-group usage statistics
- [ ] Add @mention prefix enforcement option

## Phase 4: Testing

- [ ] Mock group chat messages in tests
- [ ] Test group allowlist filtering
- [ ] Test @mention prefix detection
- [ ] Test group registration workflow

## Testing Checklist

### Unit Tests
- [ ] `test_group_chat_allowlist_filtering`
- [ ] `test_group_mention_botname_detection`
- [ ] `test_state_group_isolation`
- [ ] `test_group_registration_flow`
- [ ] `test_group_admin_commands`

### Integration Tests
- [ ] Bot responds to messages mentioning @telegram-bot in group
- [ ] Bot ignores group message without @mention
- [ ] Bot ignores messages from disallowed group
- [ ] Bot processes private messages unchanged
