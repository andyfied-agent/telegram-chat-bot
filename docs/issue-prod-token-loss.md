# Issue: Production token lost on upgrade

## Summary
When upgrading the production bot in `~/opt/telegram-chat-bot` from the repository, the `TELEGRAM_TOKEN` configuration is lost because the `.env` file is replaced during `git reset --hard`.

## Steps to Reproduce
1. Start with production bot running with `.env` file containing `TELEGRAM_TOKEN=***`
2. Run `git reset --hard v0.1.0` (or any new release)
3. Check for token: `cat .env` → file may not exist or token is missing

## Root Cause
- Production installs use `git reset --hard` which replaces all files with repository versions
- The repository's `.env.example` is a template, not a real config file
- No mechanism to preserve user-specific secrets during upgrades

## Impact
- **Critical**: Bot cannot start without token
- Users must manually re-enter sensitive credentials
- Security risk: token exposure in shell history if exported inline

## Recommended Solutions

### Option 1: Git-ignored `.env` file (Recommended)
- Add `.env` to `.gitignore`
- In `install.sh`, check if `.env` exists before copying `.env.example`
- In `telegram_chat_bot.py`, load `.env` if it exists (already implemented with `override=False`)

**Example `install.sh` fix:**
```bash
if [ ! -f .env ]; then
    cp .env.example .env
    echo "# Token not configured. Set TELEGRAM_TOKEN in .env or as environment variable." >> .env
fi
```

### Option 2: Separate secrets file
- Use `.env.secrets` or similar, always git-ignored
- `.env.example` documents all possible variables but excludes secrets

### Option 3: Configuration migration script
- Create `migrate-config.sh` that merges old `.env` with new `.env.example`
- Preserves unknown/old variables, warns about new required ones

## Acceptance Criteria
- [ ] `.env` file is git-ignored
- [ ] Upgrade process preserves existing `.env`
- [ ] New release documents migration path if breaking changes occur
- [ ] `install.sh` handles missing `.env` gracefully

## Priority
**High** - Blocking issue for any production deployment

## Related
- PR #44 (v0.1.0 release)
- Issue tracker for future releases
