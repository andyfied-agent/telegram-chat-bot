"""Tests for Issue #34: GROUP_ACCESS_MODE validation and enforcement."""
import pytest
from telegram import Update, Chat, User
from telegram.ext import ContextTypes

from telegram_chat_bot import State, Settings


class TestGroupAccessModeValidation:
    """Test GROUP_ACCESS_MODE setting validation."""
    
    def test_valid_modes_accepted(self):
        """All valid modes should be accepted."""
        for mode in ["all", "approved_users", "admins"]:
            settings = Settings(token="fake_token", group_access_mode=mode)
            assert settings.group_access_mode == mode
    
    def test_invalid_mode_rejected(self):
        """Invalid modes should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid GROUP_ACCESS_MODE"):
            Settings(token="fake_token", group_access_mode="invalid_mode")
    
    def test_invalid_mode_message(self):
        """Error message should list valid options."""
        with pytest.raises(ValueError) as exc_info:
            Settings(token="fake_token", group_access_mode="random")
        assert "all" in str(exc_info.value)
        assert "approved_users" in str(exc_info.value)
        assert "admins" in str(exc_info.value)


class TestGroupAccessModeEnforcement:
    """Test GROUP_ACCESS_MODE enforcement in allowed_group."""
    
    def test_all_mode_any_user(self):
        """In 'all' mode, any user should be able to use approved groups."""
        settings = Settings(token="fake_token", group_access_mode="all")
        state = State(settings)
        state.group_registrations.set_status(456, "approved")
        
        # Should allow without user_id
        assert state.allowed_group(456, user_id=None) is True
        # Should allow with any user_id
        assert state.allowed_group(456, user_id=123) is True
    
    def test_approved_users_mode_requires_approval(self):
        """In 'approved_users' mode, only approved users should be allowed."""
        settings = Settings(token="fake_token", group_access_mode="approved_users", allowed_user_ids=frozenset([123]))
        state = State(settings)
        state.group_registrations.set_status(456, "approved")
        
        # Approved user should be allowed
        assert state.allowed_group(456, user_id=123) is True
        # Unapproved user should NOT be allowed
        assert state.allowed_group(456, user_id=999) is False
        # Without user_id should NOT be allowed (fail closed)
        assert state.allowed_group(456, user_id=None) is False
    
    def test_admins_mode_requires_admin(self):
        """In 'admins' mode, only admins should be allowed."""
        settings = Settings(token="fake_token", group_access_mode="admins", admin_user_ids=frozenset([123]))
        state = State(settings)
        state.group_registrations.set_status(456, "approved")
        
        # Admin should be allowed
        assert state.allowed_group(456, user_id=123) is True
        # Non-admin should NOT be allowed
        assert state.allowed_group(456, user_id=999) is False
        # Without user_id should NOT be allowed (fail closed)
        assert state.allowed_group(456, user_id=None) is False
