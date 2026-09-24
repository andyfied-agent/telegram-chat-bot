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
**Status:** Open  
**Priority:** Medium  
**Description:** The bot currently only responds in private (direct) messages.  
**Solution:** Add optional group chat support with `TELEGRAM_ALLOWED_GROUP_IDS`, per-group permissions, @mention prefix.  
**Impact:** Enables bot usage in moderated group chats and team workspaces.
**Blocked:** Not started - Requires significant refactoring.

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

### 8. Group Chat Registration System
**Status:** Open  
**Description:** Groups would need their own registration/approval system.  
**Solution:** Extend `RegistrationStore` to handle group IDs.  
**Impact:** Admin control over group interactions.
**Status:** 🔴 Blocked - Depends on Issue #7.

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

| Category | Total | Complete | Remaining |
|----------|-------|----------|-----------|
| High Priority | 1 | 1 | 0 |
| Medium Priority | 3 | 2 | 1 |
| Low Priority | 3 | 2 | 1 |
| **Total** | **7** | **5** | **2** |

**Test Coverage:** 115 tests passing (100 from main + 15 new tests for concurrent limiting, rate-limit visibility, and logging)

**Next Action:** Issue #7 (Group Chat Support) requires significant refactoring; not prioritized.

---

**Last updated:** 2026-09-24
**Bot version:** Latest (all 115 tests pass, service running)
