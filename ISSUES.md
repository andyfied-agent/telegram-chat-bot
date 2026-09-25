# Telegram Chat Bot Issues

This file tracks outstanding issues for the Telegram Chat Bot project.

## Priority Order (High → Low)

### 🔴 High Priority

### 3. No Concurrent Request Limit
**Status:** Complete  
**Description:** Multiple users could hammer the llama.cpp server simultaneously without protection.  
**Solution:** Implemented `MAX_CONCURRENT_REQUESTS` setting with `asyncio.Semaphore` to limit concurrent requests.  
**Impact:** Model stability and response quality under load.  
**Implementation:** ✅ Complete - Added concurrent request semaphore with proper cleanup. All 9 tests pass (including real concurrency test with shared State).

---

### 🟡 Medium Priority

### 1. No Central Logging
**Status:** Complete  
**Description:** The bot used only stdout logging, making debugging difficult.  
**Solution:** Added rotating file handler to `/mnt/scratch/hermes/logs/telegram-chat-bot.log`.  
**Impact:** Debugging and incident response.  
**Implementation:** ✅ Complete - Rotating file handler (10MB per file, 5 backups), lazily initialized for CI compatibility.

### 2. Missing Error Metrics and Health Monitoring
**Status:** Complete  
**Description:** No way to view request statistics or error rates.  
**Solution:** Added `/metrics` command showing request stats.  
**Impact:** Operational monitoring and troubleshooting.  
**Implementation:** ✅ Complete - Shows total/successful/failed requests, success rate, avg response time, concurrent limit.

### 7. Group Chat Support
**Status:** Complete  
**Description:** Added optional group chat support with `TELEGRAM_ALLOWED_GROUP_IDS`, per-group permissions, @mention prefix.  
**Solution:** Implemented group filtering, per-group state, and group registration workflow.  
**Impact:** Enables bot usage in moderated group chats and team workspaces.
**Implementation:** ✅ Complete - Added group context management, @mention detection, and group registration commands.

---

### 🟢 Low Priority

### 4. Missing Rate-Limit Visibility
**Status:** Complete  
**Description:** No way to view rate-limit status or per-user request history.  
**Solution:** Added `/ratelimit` command showing requests in last minute, cooldown status, global concurrent limit.  
**Impact:** User experience and debugging.  
**Implementation:** ✅ Complete - All 6 tests pass.

### 5. No Active State Backup/Rotations
**Status:** Complete  
**Description:** No active backup scheduled for registration state.  
**Solution:** Enabled backup timer with 200MB limit and 180MB alert.  
**Impact:** Data durability and recovery.  
**Implementation:** ✅ Complete - Daily backups with rotation policy.

### 38. Group Context Management
**Status:** Complete  
**Description:** Group-specific history exists, but `/history` and `/reset` are private-only.  
**Solution:** Extend `/history` and `/reset` to work in groups with per-user context.  
**Impact:** Users can inspect and reset their group conversation history.  
**Implementation:** ✅ Complete - Commands now support group context with proper authorization.

### 8. Group Chat Registration System
**Status:** Complete  
**Description:** Groups need their own registration/approval system.  
**Solution:** Implemented `/startgroup`, `/approvegroup`, `/rejectgroup`, `/revokegroup` commands.  
**Impact:** Admin control over group interactions.
**Implementation:** ✅ Complete - Group registration store and approval workflow implemented.

### 33. Private Prompts Isolation
**Status:** Complete  
**Description:** Private prompts leak into group chat requests.  
**Solution:** Add group-scoped prompts and ensure private prompts are only used for private chats.  
**Impact:** Secure separation of private and group contexts.  
**Implementation:** ✅ Complete - Added `group_prompts` dict, `setgroupprompt`/`cleargroupprompt`/`showgroupprompt` commands, and updated `messages()` to use group prompts for group requests.

### 34. Configurable Group Access Policy
**Status:** Complete  
**Description:** Group access mode should be configurable (`all` vs `approved_users` vs `admins`).  
**Solution:** Add `GROUP_ACCESS_MODE` environment variable and update `allowed_group()` to enforce the policy.  
**Impact:** Flexible group authorization policies.  
**Implementation:** ✅ Complete - Added `group_access_mode` setting and updated authorization logic.

---

## Resolved Issues

The following GitHub issues have been addressed:

- **Issue #1:** Bounded llama.cpp model responses and request max_tokens
- **Issue #2:** Add `/shutdown` to bot help text for documentation consistency
- **Issue #3:** Reproducible isolated installation
- **Issue #4:** Separate admin authorization for global prompt via `TELEGRAM_ADMIN_USER_IDS`
- **Issue #12:** Persistent state and usage accounting (PR #21)
- **Registration and log secrets:** Handle start command payloads, complete registration workflow

---

## Current Status Summary

|||| Category | Total | Complete | Remaining |
||||----------|-------|----------|-----------|
|||| High Priority | 1 | 1 | 0 |
|||| Medium Priority | 4 | 4 | 0 |
|||| Low Priority | 7 | 7 | 0 |
|||| **Total** | **12** | **12** | **0** |

**Test Coverage:** 115 tests passing (100 from main + 15 new tests for concurrent limiting, rate-limit visibility, and logging)

**Next Action:** Issue #41 (Improve user-facing model and provider error messages) - minor UX enhancement.

---

**Last updated:** 2026-09-25
**Bot version:** Latest (all 115 tests pass, service running)
