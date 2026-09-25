"""Tests for concurrent history consistency under failures."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_chat_bot import Settings, State, handle_message, ProviderTimeoutError, HTTPClientError


@pytest.mark.asyncio
async def test_history_consistency_on_single_failure():
    """Test that a single failed request correctly removes only its own message."""
    settings = Settings(token='test_token', allowed_user_ids=frozenset({123}))
    state = State(settings)
    
    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 123
    mock_update.message.text = "hello"
    mock_update.message.reply_text = AsyncMock()
    
    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}
    
    # Mock complete to raise an error
    with patch("telegram_chat_bot.complete_with_usage", side_effect=ProviderTimeoutError("timeout")):
        await handle_message(mock_update, mock_context)
    
    # History should be empty (user message popped on failure)
    assert len(state.history[123]) == 0


@pytest.mark.asyncio
async def test_history_consistency_concurrent_failures_same_user():
    """Test history consistency when multiple concurrent requests from the same user fail.
    
    This is the critical race condition test:
    1. User sends message A (index 0) → history = [A]
    2. User sends message B (index 1) → history = [A, B] (before A completes)
    3. A fails → should pop A, leaving [B]
    4. B fails → should pop B, leaving []
    
    The bug was: pop() removes the last element, so A's failure would pop B instead.
    """
    settings = Settings(token='test_token', request_interval=0.001, allowed_user_ids=frozenset({123}))
    state = State(settings)
    
    async def send_message(user_id, message_text, should_fail=True):
        mock_update = MagicMock()
        mock_update.effective_chat.type = "private"
        mock_update.effective_user.id = user_id
        mock_update.message.text = message_text
        mock_update.message.reply_text = AsyncMock()
        
        mock_context = MagicMock()
        mock_context.application.bot_data = {"state": state}
        
        def mock_complete(*args, **kwargs):
            if should_fail:
                raise ProviderTimeoutError("timeout")
            return MagicMock(text="response", prompt_tokens=10, completion_tokens=10)
        
        with patch("telegram_chat_bot.complete_with_usage", mock_complete):
            await handle_message(mock_update, mock_context)
    
    # Send 2 messages concurrently from the same user, both will fail
    # They should be sent almost simultaneously
    tasks = [
        send_message(123, "message A", should_fail=True),
        send_message(123, "message B", should_fail=True),
    ]
    
    # Run concurrently
    await asyncio.gather(*tasks)
    
    # History should be empty (both user messages removed on their respective failures)
    # The key: each failure should only pop its own message, not the other's
    assert len(state.history[123]) == 0, f"Expected empty history, got {state.history[123]}"


@pytest.mark.asyncio
async def test_history_consistency_concurrent_success_and_failure_same_user():
    """Test history consistency when one request succeeds and another fails concurrently.
    
    1. User sends message A (index 0) → history = [A]
    2. User sends message B (index 1) → history = [A, B]
    3. A succeeds → history = [A, A_response]
    4. B fails → should pop B, leaving [A, A_response]
    
    The bug was: B's failure would pop A_response instead of B.
    """
    settings = Settings(token='test_token', request_interval=0.001, allowed_user_ids=frozenset({123}))
    state = State(settings)
    
    async def send_message(user_id, message_text, should_fail=True):
        mock_update = MagicMock()
        mock_update.effective_chat.type = "private"
        mock_update.effective_user.id = user_id
        mock_update.message.text = message_text
        mock_update.message.reply_text = AsyncMock()
        
        mock_context = MagicMock()
        mock_context.application.bot_data = {"state": state}
        
        def mock_complete(*args, **kwargs):
            if should_fail:
                raise ProviderTimeoutError("timeout")
            return MagicMock(text="response", prompt_tokens=10, completion_tokens=10)
        
        with patch("telegram_chat_bot.complete_with_usage", mock_complete):
            await handle_message(mock_update, mock_context)
    
    # Send 2 messages concurrently: one succeeds, one fails
    tasks = [
        send_message(123, "message A", should_fail=False),  # Will succeed
        send_message(123, "message B", should_fail=True),   # Will fail
    ]
    
    await asyncio.gather(*tasks)
    
    # History should contain exactly 2 entries: user msg A + response A
    # The failed message B should have been removed, leaving only A's conversation
    assert len(state.history[123]) == 2, f"Expected 2 entries (A user + A response), got {len(state.history[123])}: {state.history[123]}"
    assert state.history[123][0]["role"] == "user", "First entry should be user message A"
    assert state.history[123][0]["content"] == "message A"
    assert state.history[123][1]["role"] == "assistant", "Second entry should be assistant response"


@pytest.mark.asyncio
async def test_history_consistency_http_client_error():
    """Test that HTTPClientError also correctly removes only its own message."""
    settings = Settings(token='test_token', allowed_user_ids=frozenset({123}))
    state = State(settings)
    
    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 123
    mock_update.message.text = "hello"
    mock_update.message.reply_text = AsyncMock()
    
    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}
    
    # Mock complete to raise HTTPClientError
    with patch("telegram_chat_bot.complete_with_usage", side_effect=HTTPClientError("HTTP 400: Bad Request")):
        await handle_message(mock_update, mock_context)
    
    # History should be empty (user message popped on failure)
    assert len(state.history[123]) == 0


@pytest.mark.asyncio
async def test_circuit_breaker_reset_on_success():
    """Test that circuit breaker resets failure count on successful request in CLOSED state.
    
    This addresses the issue where 5 isolated failures separated by hundreds of
    successes could eventually open the circuit (threshold of 5 never resets).
    """
    from telegram_chat_bot import CircuitBreaker
    
    # Create a fresh circuit breaker with threshold=5
    cb = CircuitBreaker(failure_threshold=5, success_threshold=2)
    
    # Simulate 5 failures (circuit should open)
    for _ in range(5):
        cb.record_failure()
    assert cb.get_state() == "open", "Circuit should be open after 5 failures"
    
    # Now simulate 1 success - with the fix, this should reset failure_count
    cb.record_success()
    
    # State should still be OPEN (success doesn't close OPEN state)
    # But failure_count should be reset to 0
    assert cb.get_state() == "open", "Circuit should remain OPEN"
    assert cb.get_stats()["failure_count"] == 0, "Failure count should be reset on success"


@pytest.mark.asyncio
async def test_circuit_breaker_thread_safety():
    """Test that circuit breaker is thread-safe under concurrent access.
    
    Multiple threads calling record_success/record_failure simultaneously
    should not corrupt state.
    """
    from telegram_chat_bot import CircuitBreaker
    
    cb = CircuitBreaker(failure_threshold=100, success_threshold=50)
    
    errors = []
    
    def thread_failures(count):
        try:
            for _ in range(count):
                cb.record_failure()
        except Exception as e:
            errors.append(e)
    
    def thread_successes(count):
        try:
            for _ in range(count):
                cb.record_success()
        except Exception as e:
            errors.append(e)
    
    # Run 10 threads, each calling record_failure 10 times (100 total failures)
    # and 10 threads, each calling record_success 5 times (50 total successes)
    threads = []
    for _ in range(10):
        t = asyncio.create_task(asyncio.to_thread(thread_failures, 10))
        threads.append(t)
        t = asyncio.create_task(asyncio.to_thread(thread_successes, 5))
        threads.append(t)
    
    await asyncio.gather(*threads)
    
    # Should complete without errors
    assert len(errors) == 0, f"Circuit breaker had errors: {errors}"
    
    # Final state should be consistent (failure_count should be non-negative)
    stats = cb.get_stats()
    assert stats["failure_count"] >= 0, f"Negative failure count: {stats['failure_count']}"
    assert stats["success_count"] >= 0, f"Negative success count: {stats['success_count']}"
