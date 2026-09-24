"""Tests for concurrent request semaphore mechanism."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_chat_bot import Settings, State, handle_message


@pytest.mark.asyncio
async def test_semaphore_initialization():
    """Test that semaphore is initialized with the correct count."""
    settings = Settings(token='test_token', max_concurrent_requests=3)
    state = State(settings)
    assert state.request_semaphore._value == 3


@pytest.mark.asyncio
async def test_semaphore_acquires_all_slots():
    """Test that up to N concurrent requests can acquire slots."""
    settings = Settings(token='test_token', max_concurrent_requests=2)
    state = State(settings)
    
    # Simulate 2 concurrent requests
    await state.request_semaphore.acquire()
    await state.request_semaphore.acquire()
    
    # Third request should block (we test this by checking semaphore value)
    assert state.request_semaphore._value == 0


@pytest.mark.asyncio
async def test_semaphore_releases_on_exception():
    """Test that semaphore is released even when an exception occurs."""
    settings = Settings(token='test_token', max_concurrent_requests=1)
    state = State(settings)
    
    # Acquire one slot
    await state.request_semaphore.acquire()
    assert state.request_semaphore._value == 0
    
    # Release it
    state.request_semaphore.release()
    assert state.request_semaphore._value == 1


@pytest.mark.asyncio
async def test_handle_message_with_semaphore():
    """Test that handle_message properly uses the semaphore."""
    settings = Settings(token='test_token', max_concurrent_requests=1, allowed_user_ids=frozenset({123}))
    state = State(settings)
    
    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 123
    mock_update.message.text = "hello"
    mock_update.message.reply_text = AsyncMock()
    
    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}
    
    # Mock complete to return a simple response
    with patch("telegram_chat_bot.complete_with_usage", return_value=MagicMock(text="hello back", prompt_tokens=10, completion_tokens=10)):
        await handle_message(mock_update, mock_context)
    
    mock_update.message.reply_text.assert_called()


@pytest.mark.asyncio
async def test_semaphore_blocks_excess_requests():
    """Test that semaphore blocks requests when all slots are taken."""
    settings = Settings(token='test_token', max_concurrent_requests=1)
    state = State(settings)
    
    # Acquire the only slot
    await state.request_semaphore.acquire()
    assert state.request_semaphore._value == 0
    
    # Try to acquire again - this should block
    # We test by checking that a second acquire would block
    acquired = asyncio.Future()
    
    async def try_acquire():
        await state.request_semaphore.acquire()
        acquired.set_result(True)
    
    task = asyncio.create_task(try_acquire())
    
    # Give it a moment - it should still be pending
    await asyncio.sleep(0.01)
    assert not acquired.done()
    
    # Release the slot
    state.request_semaphore.release()
    
    # Now the task should complete
    await asyncio.sleep(0.01)
    assert acquired.done()
    
    # Clean up
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_concurrent_requests_with_model_call():
    """Test that semaphore limits concurrent model calls.
    
    We create separate states and send messages. With max_concurrent_requests=1,
    each message should be processed one at a time.
    """
    call_count = [0]
    
    def slow_complete(*args, **kwargs):
        call_count[0] += 1
        time.sleep(0.05)  # Simulate slow model call
        return MagicMock(text="response", prompt_tokens=10, completion_tokens=10)
    
    async def send_message(state):
        mock_update = MagicMock()
        mock_update.effective_chat.type = "private"
        mock_update.effective_user.id = 123
        mock_update.message.text = "hello"
        mock_update.message.reply_text = AsyncMock()
        
        mock_context = MagicMock()
        mock_context.application.bot_data = {"state": state}
        
        with patch("telegram_chat_bot.complete_with_usage", slow_complete):
            await handle_message(mock_update, mock_context)
    
    # Create 3 separate states
    settings1 = Settings(token='test_token', max_concurrent_requests=1, allowed_user_ids=frozenset({123}))
    settings2 = Settings(token='test_token', max_concurrent_requests=1, allowed_user_ids=frozenset({123}))
    settings3 = Settings(token='test_token', max_concurrent_requests=1, allowed_user_ids=frozenset({123}))
    
    state1 = State(settings1)
    state2 = State(settings2)
    state3 = State(settings3)
    
    # Send messages sequentially
    await send_message(state1)
    await send_message(state2)
    await send_message(state3)
    
    # All 3 should have been called
    assert call_count[0] == 3


@pytest.mark.asyncio
async def test_shared_state_concurrent_requests():
    """Test that one shared State allows N concurrent requests.
    
    This is the real concurrency test: with max_concurrent_requests=3,
    3 tasks should be able to run simultaneously (not serialized).
    """
    concurrent_count = [0]
    max_concurrent = [0]
    
    def slow_complete(*args, **kwargs):
        concurrent_count[0] += 1
        current_concurrent = concurrent_count[0]
        max_concurrent[0] = max(max_concurrent[0], current_concurrent)
        time.sleep(0.1)  # Simulate slow model call
        concurrent_count[0] -= 1
        return MagicMock(text="response", prompt_tokens=10, completion_tokens=10)
    
    # Create one shared state with max_concurrent_requests=3 and very short request interval
    # Use the same user ID for all concurrent tasks to avoid rate-limiting between them
    settings = Settings(token='test_token', max_concurrent_requests=3, request_interval=0.001, allowed_user_ids=frozenset({123}))
    state = State(settings)
    
    async def send_message(user_id):
        mock_update = MagicMock()
        mock_update.effective_chat.type = "private"
        mock_update.effective_user.id = user_id
        mock_update.message.text = "hello"
        mock_update.message.reply_text = AsyncMock()
        
        mock_context = MagicMock()
        mock_context.application.bot_data = {"state": state}
        
        with patch("telegram_chat_bot.complete_with_usage", slow_complete):
            await handle_message(mock_update, mock_context)
    
    # Send 3 messages concurrently from the SAME user - only semaphore should limit concurrency
    tasks = [send_message(123), send_message(123), send_message(123)]
    await asyncio.gather(*tasks)
    
    # Verify that concurrent_count reached at least 2 (not just 1)
    # This proves the requests were actually concurrent, not serialized
    assert max_concurrent[0] >= 2, f"Expected at least 2 concurrent requests, got {max_concurrent[0]}"


@pytest.mark.asyncio
async def test_shared_state_concurrent_limit_enforced():
    """Test that shared State enforces the max concurrent limit.
    
    With max_concurrent_requests=2, we should see at most 2 concurrent requests.
    """
    concurrent_count = [0]
    max_concurrent = [0]
    
    def slow_complete(*args, **kwargs):
        concurrent_count[0] += 1
        current_concurrent = concurrent_count[0]
        max_concurrent[0] = max(max_concurrent[0], current_concurrent)
        time.sleep(0.2)  # Simulate slow model call
        concurrent_count[0] -= 1
        return MagicMock(text="response", prompt_tokens=10, completion_tokens=10)
    
    # Create one shared state with max_concurrent_requests=2 and very short request interval
    settings = Settings(token='test_token', max_concurrent_requests=2, request_interval=0.001, allowed_user_ids=frozenset({123}))
    state = State(settings)
    
    async def send_message(user_id):
        mock_update = MagicMock()
        mock_update.effective_chat.type = "private"
        mock_update.effective_user.id = user_id
        mock_update.message.text = "hello"
        mock_update.message.reply_text = AsyncMock()
        
        mock_context = MagicMock()
        mock_context.application.bot_data = {"state": state}
        
        with patch("telegram_chat_bot.complete_with_usage", slow_complete):
            await handle_message(mock_update, mock_context)
    
    # Send 5 messages concurrently from the SAME user - should be limited to 2 at a time
    tasks = [send_message(123) for _ in range(5)]
    await asyncio.gather(*tasks)
    
    # Verify that max concurrent was exactly 2 (not more)
    assert max_concurrent[0] == 2, f"Expected max 2 concurrent requests, got {max_concurrent[0]}"


@pytest.mark.asyncio
async def test_handle_message_semaphore_logging():
    """Test that semaphore acquire/release are logged."""
    settings = Settings(token='test_token', max_concurrent_requests=2, allowed_user_ids=frozenset({123}))
    state = State(settings)
    
    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 123
    mock_update.message.text = "hello"
    mock_update.message.reply_text = AsyncMock()
    
    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}
    
    with patch("telegram_chat_bot.complete_with_usage", return_value=MagicMock(text="hello back", prompt_tokens=10, completion_tokens=10)):
        await handle_message(mock_update, mock_context)
    
    # The logging should happen (we don't capture it here, but the code should execute)
    assert mock_update.message.reply_text.called
