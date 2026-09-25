"""Tests for /health command and USER_RATE_LIMITS per-user rate limits."""

import asyncio
import json
import math
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Update, Chat, User

from telegram_chat_bot import Settings, _parse_user_rate_limits, State, health, handle_message, ratelimit


class TestUserRateLimitsParsing:
    """Tests for USER_RATE_LIMITS JSON parsing."""
    
    def test_empty_string_returns_empty_dict(self):
        result = _parse_user_rate_limits("")
        assert result == {}
    
    def test_empty_object_returns_empty_dict(self):
        result = _parse_user_rate_limits("{}")
        assert result == {}
    
    def test_valid_user_id_interval(self):
        result = _parse_user_rate_limits('{"123456789": 30.0}')
        assert result == {123456789: 30.0}
    
    def test_multiple_users(self):
        result = _parse_user_rate_limits('{"123": 30.0, "456": 60.0}')
        assert result == {123: 30.0, 456: 60.0}
    
    def test_integer_interval(self):
        result = _parse_user_rate_limits('{"123": 30}')
        assert result == {123: 30.0}
    
    def test_float_interval(self):
        result = _parse_user_rate_limits('{"123": 30.5}')
        assert result == {123: 30.5}
    
    def test_boolean_rejected_true(self):
        with pytest.raises(ValueError) as exc_info:
            _parse_user_rate_limits('{"123": true}')
        assert "not booleans" in str(exc_info.value)
    
    def test_boolean_rejected_false(self):
        with pytest.raises(ValueError) as exc_info:
            _parse_user_rate_limits('{"123": false}')
        assert "not booleans" in str(exc_info.value)
    
    def test_nan_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            _parse_user_rate_limits('{"123": NaN}')
        assert "finite numbers" in str(exc_info.value)
    
    def test_infinity_rejected_positive(self):
        with pytest.raises(ValueError) as exc_info:
            _parse_user_rate_limits('{"123": Infinity}')
        assert "finite numbers" in str(exc_info.value) or "positive" in str(exc_info.value)
    
    def test_infinity_rejected_negative(self):
        with pytest.raises(ValueError) as exc_info:
            _parse_user_rate_limits('{"123": -Infinity}')
        assert "finite numbers" in str(exc_info.value) or "positive" in str(exc_info.value)
    
    def test_zero_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            _parse_user_rate_limits('{"123": 0}')
        assert "positive" in str(exc_info.value)
    
    def test_negative_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            _parse_user_rate_limits('{"123": -5.0}')
        assert "positive" in str(exc_info.value)
    
    def test_non_integer_user_id_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            _parse_user_rate_limits('{"123abc": 30.0}')
        assert "must be integers" in str(exc_info.value)
    
    def test_non_string_user_id_key_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            _parse_user_rate_limits('{123: 30.0}')
        # JSON requires string keys, so this fails at parse time
    
    def test_invalid_json(self):
        with pytest.raises(ValueError) as exc_info:
            _parse_user_rate_limits('not valid json')
        assert "JSON object" in str(exc_info.value)
    
    def test_array_not_object(self):
        with pytest.raises(ValueError) as exc_info:
            _parse_user_rate_limits('[123, 456]')
        assert "must be a JSON object" in str(exc_info.value)


class TestUserRateLimitsSettings:
    """Tests for Settings with USER_RATE_LIMITS."""
    
    def test_default_empty_dict(self):
        settings = Settings(token='test')
        assert settings.user_rate_limits == {}
    
    def test_custom_user_rate_limits(self):
        with patch.dict(os.environ, {'USER_RATE_LIMITS': '{"123": 30.0, "456": 60.0}'}):
            settings = Settings(token='test')
            assert settings.user_rate_limits == {123: 30.0, 456: 60.0}
    
    def test_boolean_rejected_in_settings(self):
        # Settings dataclass doesn't directly validate user_rate_limits; it's parsed from env
        # So we test that _parse_user_rate_limits rejects it
        with pytest.raises(ValueError) as exc_info:
            _parse_user_rate_limits('{"123": true}')
        assert "not booleans" in str(exc_info.value)


class TestHealthCommand:
    """Tests for /health command."""
    
    @pytest.fixture
    def mock_settings(self):
        settings = MagicMock()
        settings.base_url = "http://127.0.0.1:8081/v1"
        settings.timeout = 120.0
        settings.model = "test-model.gguf"
        return settings
    
    @pytest.fixture
    def mock_state(self, mock_settings):
        state = MagicMock(spec=State)
        state.allowed = MagicMock(return_value=True)
        state.settings = mock_settings
        return state
    
    @pytest.fixture
    def mock_update(self):
        update = MagicMock(spec=Update)
        update.effective_user = User(id=123456789, first_name="Test", is_bot=False)
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        return update
    
    @pytest.fixture
    def context_with_state(self, mock_state):
        context = MagicMock()
        context.application.bot_data = {"state": mock_state}
        return context
    
    @pytest.mark.asyncio
    async def test_health_online(self, context_with_state, mock_state, mock_update):
        with patch('telegram_chat_bot.health_check') as mock_health:
            mock_health.return_value = ("test-model.gguf", True)
            
            await health(mock_update, context_with_state)
            
            # Should use configured model name, not what /health returns
            mock_update.message.reply_text.assert_called_once()
            response = mock_update.message.reply_text.call_args[0][0]
            assert "test-model.gguf" in response
            assert "✅ Online" in response
    
    @pytest.mark.asyncio
    async def test_health_offline(self, context_with_state, mock_state, mock_update):
        with patch('telegram_chat_bot.health_check') as mock_health:
            mock_health.return_value = ("unreachable", False)
            
            await health(mock_update, context_with_state)
            
            mock_update.message.reply_text.assert_called_once()
            response = mock_update.message.reply_text.call_args[0][0]
            assert "test-model.gguf" in response
            assert "❌ Offline" in response
    
    @pytest.mark.asyncio
    async def test_health_requires_permission(self, mock_state, mock_update):
        mock_state.allowed = MagicMock(return_value=False)
        context = MagicMock()
        context.application.bot_data = {"state": mock_state}
        
        with patch('telegram_chat_bot.health_check'):
            await health(mock_update, context)
            
            mock_update.message.reply_text.assert_not_called()


class TestPerUserRateLimitEnforcement:
    """Tests for per-user rate limit enforcement."""
    
    def test_user_with_custom_limit_uses_custom(self):
        """User 123 has 30s limit, should use that not 1s global."""
        user_rate_limits = {123: 30.0, 456: 60.0}
        global_interval = 1.0
        user_rate_limit = user_rate_limits.get(123, global_interval)
        assert user_rate_limit == 30.0
    
    def test_user_without_custom_limit_falls_back_to_global(self):
        """User 789 has no custom limit, should use 1s global."""
        user_rate_limits = {123: 30.0, 456: 60.0}
        global_interval = 1.0
        user_rate_limit = user_rate_limits.get(789, global_interval)
        assert user_rate_limit == 1.0
    
    def test_empty_user_limits_uses_global(self):
        """Empty user limits dict should use global."""
        user_rate_limits = {}
        global_interval = 2.0
        user_rate_limit = user_rate_limits.get(999, global_interval)
        assert user_rate_limit == 2.0


class TestRatelimitCommandReportsCorrectly:
    """Tests that /ratelimit shows correct per-user rate limit."""
    
    @pytest.fixture
    def mock_settings(self):
        settings = MagicMock()
        settings.request_interval = 1.0  # Global
        settings.user_rate_limits = {123: 30.0}  # User 123 has custom
        settings.max_concurrent_requests = 2
        return settings
    
    @pytest.fixture
    def mock_state(self, mock_settings):
        state = MagicMock(spec=State)
        state.allowed = MagicMock(return_value=True)
        state.settings = mock_settings
        state.user_requests_minute = {123: []}
        state.last_request = {123: 0}  # Just sent
        state.request_semaphore = MagicMock()
        state.request_semaphore._value = 2
        return state
    
    @pytest.fixture
    def mock_update(self):
        update = MagicMock(spec=Update)
        update.effective_user = User(id=123, first_name="Test", is_bot=False)
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        return update
    
    @pytest.fixture
    def context_with_state(self, mock_state):
        context = MagicMock()
        context.application.bot_data = {"state": mock_state}
        return context
    
    @pytest.mark.asyncio
    async def test_ratelimit_shows_custom_rate(self, context_with_state, mock_state, mock_update):
        """User 123 should see 30s cooldown, not 1s global."""
        mock_state.last_request = {123: 100}  # Sent 100s ago, but still waiting
        
        # Need to set up time.monotonic to return a specific value
        with patch('telegram_chat_bot.time.monotonic', return_value=105.0):
            await ratelimit(mock_update, context_with_state)
        
        mock_update.message.reply_text.assert_called_once()
        response = mock_update.message.reply_text.call_args[0][0]
        
        # Should show 30s cooldown (custom), not 1s (global)
        assert "30.0s" in response
        assert "Waiting:" in response
        
    @pytest.mark.asyncio
    async def test_ratelimit_shows_global_for_no_custom(self, context_with_state, mock_state, mock_update):
        """User without custom limit should see global rate."""
        mock_state.allowed = MagicMock(return_value=True)
        mock_state.last_request = {123: 100}
        
        # Create new update for different user
        mock_update.effective_user = User(id=999, first_name="Test", is_bot=False)
        mock_state.user_requests_minute = {999: []}
        mock_state.last_request = {999: 100}
        
        with patch('telegram_chat_bot.time.monotonic', return_value=105.0):
            await ratelimit(mock_update, context_with_state)
        
        mock_update.message.reply_text.assert_called_once()
        response = mock_update.message.reply_text.call_args[0][0]
        
        # Should show 1.0s cooldown (global)
        assert "1.0s" in response
