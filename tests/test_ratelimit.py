"""Tests for rate-limit visibility feature."""
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_chat_bot import Settings, State, ratelimit


@pytest.mark.asyncio
async def test_ratelimit_initial_state():
    """Test that /ratelimit shows correct initial state."""
    settings = Settings(token='test', allowed_user_ids=frozenset({123}), request_interval=1.0)
    state = State(settings)
    
    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 123
    mock_update.message.reply_text = AsyncMock()
    
    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}
    
    await ratelimit(mock_update, mock_context)
    
    mock_update.message.reply_text.assert_called_once()
    response = mock_update.message.reply_text.call_args[0][0]
    assert "Requests in last minute: 0" in response
    assert "Ready to send" in response


@pytest.mark.asyncio
async def test_ratelimit_with_recent_requests():
    """Test that /ratelimit shows request count in last minute."""
    settings = Settings(token='test', allowed_user_ids=frozenset({123}), request_interval=1.0)
    state = State(settings)
    
    # Simulate 5 requests in the last minute
    now = time.monotonic()
    for i in range(5):
        state.user_requests_minute[123].append(now - i * 5)  # Every 5 seconds
    state.last_request[123] = now
    
    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 123
    mock_update.message.reply_text = AsyncMock()
    
    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}
    
    await ratelimit(mock_update, mock_context)
    
    response = mock_update.message.reply_text.call_args[0][0]
    assert "Requests in last minute: 5" in response


@pytest.mark.asyncio
async def test_ratelimit_with_old_requests_filtered():
    """Test that requests older than 1 minute are filtered out."""
    settings = Settings(token='test', allowed_user_ids=frozenset({123}), request_interval=1.0)
    state = State(settings)
    
    # Simulate 5 requests older than 1 minute
    now = time.monotonic()
    for i in range(5):
        state.user_requests_minute[123].append(now - 70 - i * 5)  # All > 1 min ago
    
    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 123
    mock_update.message.reply_text = AsyncMock()
    
    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}
    
    await ratelimit(mock_update, mock_context)
    
    response = mock_update.message.reply_text.call_args[0][0]
    assert "Requests in last minute: 0" in response


@pytest.mark.asyncio
async def test_ratelimit_waiting_for_cooldown():
    """Test that /ratelimit shows wait time when cooldown active."""
    settings = Settings(token='test', allowed_user_ids=frozenset({123}), request_interval=5.0)
    state = State(settings)
    
    # Last request was 2 seconds ago
    state.last_request[123] = time.monotonic() - 2
    
    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 123
    mock_update.message.reply_text = AsyncMock()
    
    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}
    
    await ratelimit(mock_update, mock_context)
    
    response = mock_update.message.reply_text.call_args[0][0]
    assert "Waiting:" in response
    assert "3.0s" in response  # 5.0 - 2.0 = 3.0s


@pytest.mark.asyncio
async def test_ratelimit_shows_global_status():
    """Test that /ratelimit shows global concurrent limit status."""
    settings = Settings(token='test', allowed_user_ids=frozenset({123}), max_concurrent_requests=3)
    state = State(settings)
    
    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 123
    mock_update.message.reply_text = AsyncMock()
    
    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}
    
    await ratelimit(mock_update, mock_context)
    
    response = mock_update.message.reply_text.call_args[0][0]
    assert "Max concurrent: 3" in response
    assert "Available slots: 3" in response


@pytest.mark.asyncio
async def test_ratelimit_denies_unallowed_user():
    """Test that /ratelimit returns early for unallowed users."""
    settings = Settings(token='test', allowed_user_ids=frozenset())  # No allowed users
    state = State(settings)
    
    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 999  # Not allowed
    mock_update.message.reply_text = AsyncMock()
    
    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}
    
    await ratelimit(mock_update, mock_context)
    
    # No reply should be sent
    mock_update.message.reply_text.assert_not_called()
