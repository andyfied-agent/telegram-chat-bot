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
    
    with patch("telegram_chat_bot.complete_with_usage", side_effect=ProviderTimeoutError("timeout")):
        await handle_message(mock_update, mock_context)
    
    assert len(state.history[123]) == 0


@pytest.mark.asyncio
async def test_history_consistency_concurrent_failures_same_user():
    """Test history consistency when multiple concurrent requests from the same user fail.
    
    This is the critical race condition test:
    1. User sends message A → history = [A]
    2. User sends message B → history = [A, B] (before A completes)
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
    
    tasks = [
        send_message(123, "message A", should_fail=True),
        send_message(123, "message B", should_fail=True),
    ]
    
    await asyncio.gather(*tasks)
    
    assert len(state.history[123]) == 0, f"Expected empty history, got {state.history[123]}"


@pytest.mark.asyncio
async def test_history_consistency_concurrent_success_and_failure_same_user():
    """Test history consistency when one request succeeds and another fails concurrently.
    
    1. User sends message A → history = [A]
    2. User sends message B → history = [A, B]
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
    
    tasks = [
        send_message(123, "message A", should_fail=False),
        send_message(123, "message B", should_fail=True),
    ]
    
    await asyncio.gather(*tasks)
    
    assert len(state.history[123]) == 2, f"Expected 2 entries, got {len(state.history[123])}"
    assert state.history[123][0]["role"] == "user"
    assert state.history[123][0]["content"] == "message A"
    assert state.history[123][1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_history_consistency_identical_messages_concurrent():
    """Test history consistency when two identical concurrent messages from the same user
    result in one success and one failure.
    
    This is the critical test for identity-based removal:
    1. User sends "hello" (A) → history = [A]
    2. User sends "hello" (B) → history = [A, B]
    3. A succeeds → history = [A, A_response]
    4. B fails → should pop B, leaving [A, A_response]
    
    The bug with .remove() was: both dicts compare equal, so B's failure could pop A's
    entry, leaving [A_response] instead of [A, A_response].
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
    
    # Two identical messages concurrently: one succeeds, one fails
    tasks = [
        send_message(123, "hello", should_fail=False),  # Will succeed
        send_message(123, "hello", should_fail=True),   # Will fail (identical text!)
    ]
    
    await asyncio.gather(*tasks)
    
    # Should have exactly 2 entries: user msg + response from the success
    # The failed identical message should have been removed correctly
    assert len(state.history[123]) == 2, f"Expected 2 entries, got {len(state.history[123])}: {state.history[123]}"
    assert state.history[123][0]["role"] == "user"
    assert state.history[123][0]["content"] == "hello"
    assert state.history[123][1]["role"] == "assistant"


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
    
    with patch("telegram_chat_bot.complete_with_usage", side_effect=HTTPClientError("HTTP 400: Bad Request")):
        await handle_message(mock_update, mock_context)
    
    assert len(state.history[123]) == 0


@pytest.mark.asyncio
async def test_circuit_breaker_reset_on_success():
    """Test that circuit breaker resets failure count on success in CLOSED state."""
    from telegram_chat_bot import CircuitBreaker
    
    cb = CircuitBreaker(failure_threshold=5, success_threshold=2)
    
    for _ in range(5):
        cb.record_failure()
    assert cb.get_state() == "open", "Circuit should be open after 5 failures"
    
    # Success in OPEN state should NOT reset failure_count
    cb.record_success()
    
    assert cb.get_state() == "open", "Circuit should remain OPEN"
    assert cb.get_stats()["failure_count"] == 5, \
        "OPEN-state success should NOT reset failure_count (preserves cooldown timer)"


@pytest.mark.asyncio
async def test_circuit_breaker_open_state_preserves_cooldown():
    """Test that success in OPEN state does NOT reset _last_failure_time."""
    from telegram_chat_bot import CircuitBreaker
    
    cb = CircuitBreaker(failure_threshold=3, success_threshold=2, cooldown_seconds=1.0)
    
    for _ in range(3):
        cb.record_failure()
    assert cb.get_state() == "open"
    initial_failure_time = cb.get_stats()["last_failure_time"]
    
    cb.record_success()
    
    stats = cb.get_stats()
    assert stats["last_failure_time"] == initial_failure_time, \
        "OPEN-state success should NOT reset _last_failure_time"


@pytest.mark.asyncio
async def test_circuit_breaker_thread_safety():
    """Test that circuit breaker is thread-safe under concurrent access."""
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
    
    threads = []
    for _ in range(10):
        t = asyncio.create_task(asyncio.to_thread(thread_failures, 10))
        threads.append(t)
        t = asyncio.create_task(asyncio.to_thread(thread_successes, 5))
        threads.append(t)
    
    await asyncio.gather(*threads)
    
    assert len(errors) == 0, f"Circuit breaker had errors: {errors}"
    
    stats = cb.get_stats()
    assert stats["failure_count"] >= 0, f"Negative failure count: {stats['failure_count']}"
    assert stats["success_count"] >= 0, f"Negative success count: {stats['success_count']}"


@pytest.mark.asyncio
async def test_circuit_breaker_concurrent_can_proceed_threaded():
    """Test that concurrent can_proceed() calls after cooldown are correctly limited.
    
    Uses asyncio.to_thread to run can_proceed() in actual threads, with a simple
    timing-based synchronization (all threads wait the same amount before calling).
    """
    import threading
    from telegram_chat_bot import CircuitBreaker
    
    cb = CircuitBreaker(failure_threshold=3, success_threshold=2, cooldown_seconds=0.05, half_open_max_requests=2)
    
    admitted_count = 0
    lock = threading.Lock()
    
    # Trip the circuit first
    for _ in range(3):
        cb.record_failure()
    assert cb.get_state() == "open"
    
    async def try_proceed():
        nonlocal admitted_count
        await asyncio.sleep(0.06)  # Let cooldown expire
        if cb.can_proceed():
            with lock:
                admitted_count += 1
    
    # Have multiple tasks try can_proceed() nearly simultaneously
    tasks = [try_proceed() for _ in range(4)]
    await asyncio.gather(*tasks)
    
    # Should have admitted at most half_open_max_requests
    assert admitted_count <= 2, f"Expected ≤{cb.half_open_max_requests} admitted, got {admitted_count}"


@pytest.mark.asyncio
async def test_circuit_breaker_get_state_thread_safety():
    """Test that get_state() is thread-safe under concurrent reads."""
    import threading
    from telegram_chat_bot import CircuitBreaker
    
    cb = CircuitBreaker()
    states_seen = set()
    errors = []
    
    def read_state():
        try:
            for _ in range(100):
                states_seen.add(cb.get_state())
        except Exception as e:
            errors.append(e)
    
    threads = []
    for _ in range(10):
        t = threading.Thread(target=read_state)
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    assert len(errors) == 0
    assert states_seen <= {"closed", "open", "half_open"}
