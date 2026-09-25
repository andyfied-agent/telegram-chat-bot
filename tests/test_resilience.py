"""Tests for resilience features."""
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import json
import pytest
import time
from urllib.error import URLError, HTTPError

from telegram_chat_bot import (
    CircuitBreaker,
    CircuitState,
    RetryConfig,
    Settings,
    _provider_failures,
    _provider_circuit_breakers,
    complete_with_usage,
    get_all_circuit_breaker_stats,
    get_circuit_breaker,
    get_provider_failures,
    handle_message,
    HTTPClientError,
    ProviderTimeoutError,
    State,
    log_provider_failure,
)


@pytest.fixture(autouse=True)
def reset_circuit_breakers():
    """Reset circuit breaker state between tests."""
    _provider_circuit_breakers.clear()
    yield
    _provider_circuit_breakers.clear()


def test_circuit_breaker_closed_state():
    cb = CircuitBreaker()
    assert cb._state == CircuitState.CLOSED
    assert cb.get_state() == "closed"


def test_circuit_breaker_opens_after_threshold():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb._state == CircuitState.OPEN


def test_circuit_breaker_rejects_when_open():
    cb = CircuitBreaker(failure_threshold=2)
    cb.record_failure()
    cb.record_failure()
    assert not cb.can_proceed()


def test_circuit_breaker_half_open_after_cooldown():
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.1)
    cb.record_failure()
    time.sleep(0.2)
    assert cb.can_proceed()
    assert cb._state == CircuitState.HALF_OPEN


def test_circuit_breaker_half_open_closes_on_success():
    cb = CircuitBreaker(failure_threshold=1, success_threshold=2, cooldown_seconds=0.01)
    cb.record_failure()
    time.sleep(0.1)
    cb.can_proceed()
    cb.record_success()
    cb.record_success()
    assert cb._state == CircuitState.CLOSED


def test_circuit_breaker_half_open_limit():
    cb = CircuitBreaker(failure_threshold=1, half_open_max_requests=2, cooldown_seconds=0.01)
    cb.record_failure()
    time.sleep(0.1)
    assert cb.can_proceed()
    assert cb._half_open_requests == 1
    assert cb.can_proceed()
    assert cb._half_open_requests == 2
    assert not cb.can_proceed()


def test_retry_config_defaults():
    rc = RetryConfig()
    assert rc.max_retries == 3
    assert rc.base_delay == 1.0
    assert rc.max_delay == 30.0
    assert rc.jitter is True


def test_settings_resilience_defaults():
    settings = Settings(token='test')
    assert settings.circuit_breaker_failure_threshold == 5
    assert settings.circuit_breaker_success_threshold == 2
    assert settings.circuit_breaker_cooldown_seconds == 60.0
    assert settings.max_retries == 3
    assert settings.retry_base_delay == 1.0
    assert settings.retry_max_delay == 30.0


def test_settings_resilience_custom():
    with patch.dict('os.environ', {
        'CIRCUIT_BREAKER_FAILURE_THRESHOLD': '10',
        'CIRCUIT_BREAKER_COOLDOWN_SECONDS': '120.0',
        'MAX_RETRIES': '5',
    }):
        settings = Settings(token='test')
    assert settings.circuit_breaker_failure_threshold == 10
    assert settings.circuit_breaker_cooldown_seconds == 120.0
    assert settings.max_retries == 5


def test_get_circuit_breaker_creates_new():
    _provider_circuit_breakers.clear()
    cb1 = get_circuit_breaker("provider1")
    cb2 = get_circuit_breaker("provider1")
    assert cb1 is cb2


def test_get_all_circuit_breaker_stats():
    _provider_circuit_breakers.clear()
    cb = get_circuit_breaker("test")
    cb.record_failure()
    stats = get_all_circuit_breaker_stats()
    assert "test" in stats
    assert stats["test"]["failure_count"] == 1


def test_log_provider_failure():
    _provider_failures.clear()
    log_provider_failure("test", "timeout")
    failures = get_provider_failures()
    assert len(failures) == 1
    assert "http" not in str(failures[0])


def test_complete_with_usage_retries_on_retryable_error():
    """Verify complete_with_usage retries on URLError."""
    call_counter = {"count": 0}
    
    def mock_urlopen(*args, **kwargs):
        call_counter["count"] += 1
        if call_counter["count"] < 4:
            from urllib.error import URLError
            raise URLError("transient failure")
        # Fourth call succeeds - return a proper mock response object
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "response"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10}
        }).encode()
        # Make it work as a context manager
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        return mock_response
    
    settings = Settings(token='test')
    messages = [{"role": "user", "content": "hello"}]
    
    with patch('telegram_chat_bot.urlopen', side_effect=mock_urlopen):
        result = complete_with_usage(settings, messages)
    
    assert call_counter["count"] == 4
    assert result.text == "response"


def test_complete_with_usage_trips_circuit_breaker():
    """Verify circuit breaker trips after failure threshold."""
    # Use failure_threshold=3 so it trips after 3 failures
    settings = Settings(token='test', circuit_breaker_failure_threshold=3, max_retries=3)
    messages = [{"role": "user", "content": "hello"}]
    
    call_count = 0
    
    def mock_urlopen(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise URLError("connection failed")
    
    with patch('telegram_chat_bot.urlopen', side_effect=mock_urlopen):
        with pytest.raises(ProviderTimeoutError):
            complete_with_usage(settings, messages)
    
    # Circuit breaker will trip after 3 failures, which happens on the 3rd attempt
    # (1 initial + 2 retries = 3 attempts, then circuit trips)
    assert call_count == 3
    cb = _provider_circuit_breakers.get(settings.provider)
    assert cb is not None
    assert cb.get_state() == "open"


def test_complete_with_usage_respects_open_circuit_breaker():
    """Verify complete_with_usage rejects requests when circuit is open."""
    settings = Settings(token='test')
    messages = [{"role": "user", "content": "hello"}]
    
    # First, trip the circuit breaker
    def mock_urlopen_fail(*args, **kwargs):
        raise URLError("connection failed")
    
    with patch('telegram_chat_bot.urlopen', side_effect=mock_urlopen_fail):
        with pytest.raises(ProviderTimeoutError):
            complete_with_usage(settings, messages)
    
    # Now try again - should fail immediately due to open circuit
    with patch('telegram_chat_bot.urlopen', side_effect=mock_urlopen_fail):
        with pytest.raises(ProviderTimeoutError) as exc_info:
            complete_with_usage(settings, messages)
        assert "Circuit breaker OPEN" in str(exc_info.value)


def test_complete_with_usage_records_timeout_failure():
    """Verify FuturesTimeoutError triggers circuit breaker failure recording."""
    settings = Settings(token='test', timeout=0.01)  # Very short timeout
    messages = [{"role": "user", "content": "hello"}]
    
    def mock_urlopen(*args, **kwargs):
        import time
        time.sleep(0.1)  # Longer than timeout
        return MagicMock()
    
    with patch('telegram_chat_bot.urlopen', side_effect=mock_urlopen):
        with pytest.raises(ProviderTimeoutError):
            complete_with_usage(settings, messages)
    
    # Verify failure was recorded
    cb = _provider_circuit_breakers.get(settings.provider)
    assert cb is not None
    assert cb._failure_count >= 1


def test_complete_with_usage_max_retries_exceeded():
    """Verify complete_with_usage raises after max retries."""
    settings = Settings(token='test')
    messages = [{"role": "user", "content": "hello"}]
    
    call_count = 0
    
    def mock_urlopen(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise URLError("connection failed")
    
    with patch('telegram_chat_bot.urlopen', side_effect=mock_urlopen):
        with pytest.raises(ProviderTimeoutError) as exc_info:
            complete_with_usage(settings, messages)
        assert "failed after 3 retries" in str(exc_info.value)
    
    assert call_count == 4  # 1 initial + 3 retries


def test_http_400_not_retryable():
    """400 Bad Request should NOT be retried."""
    call_count = 0
    
    def mock_urlopen(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        http_error = HTTPError("http://test", 400, "Bad Request", {}, None)
        raise http_error
    
    settings = Settings(token='test')
    messages = [{"role": "user", "content": "hello"}]
    
    with patch('telegram_chat_bot.urlopen', side_effect=mock_urlopen):
        with pytest.raises(HTTPClientError) as exc_info:
            complete_with_usage(settings, messages)
    
    # Should fail immediately, not retry
    assert call_count == 1
    assert "HTTP 400" in str(exc_info.value)


def test_http_401_not_retryable():
    """401 Unauthorized should NOT be retried."""
    call_count = 0
    
    def mock_urlopen(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        http_error = HTTPError("http://test", 401, "Unauthorized", {}, None)
        raise http_error
    
    settings = Settings(token='test')
    messages = [{"role": "user", "content": "hello"}]
    
    with patch('telegram_chat_bot.urlopen', side_effect=mock_urlopen):
        with pytest.raises(HTTPClientError) as exc_info:
            complete_with_usage(settings, messages)
    
    assert call_count == 1
    assert "HTTP 401" in str(exc_info.value)


def test_http_500_retryable():
    """500 Internal Server Error SHOULD be retried."""
    call_count = 0
    
    def mock_urlopen(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            http_error = HTTPError("http://test", 500, "Internal Server Error", {}, None)
            raise http_error
        # Second call succeeds
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "response"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10}
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        return mock_response
    
    settings = Settings(token='test')
    messages = [{"role": "user", "content": "hello"}]
    
    with patch('telegram_chat_bot.urlopen', side_effect=mock_urlopen):
        result = complete_with_usage(settings, messages)
    
    # Should retry once and succeed
    assert call_count == 2
    assert result.text == "response"


def test_http_503_retryable():
    """503 Service Unavailable SHOULD be retried."""
    call_count = 0
    
    def mock_urlopen(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            http_error = HTTPError("http://test", 503, "Service Unavailable", {}, None)
            raise http_error
        # Second call succeeds
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "response"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10}
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        return mock_response
    
    settings = Settings(token='test')
    messages = [{"role": "user", "content": "hello"}]
    
    with patch('telegram_chat_bot.urlopen', side_effect=mock_urlopen):
        result = complete_with_usage(settings, messages)
    
    assert call_count == 2
    assert result.text == "response"


def test_http_400_raises_client_error():
    """Verify HTTP 400 raises HTTPClientError, not retried."""
    call_count = 0
    
    def mock_urlopen(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        http_error = HTTPError("http://test", 400, "Bad Request", {}, None)
        raise http_error
    
    settings = Settings(token='test')
    
    with patch('telegram_chat_bot.urlopen', side_effect=mock_urlopen):
        with pytest.raises(HTTPClientError) as exc_info:
            complete_with_usage(settings, [{"role": "user", "content": "hello"}])
    
    assert call_count == 1
    assert "HTTP 400" in str(exc_info.value)


def test_http_500_retries():
    """Verify HTTP 500 is retried and succeeds on second attempt."""
    call_count = 0
    
    def mock_urlopen(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            http_error = HTTPError("http://test", 500, "Internal Server Error", {}, None)
            raise http_error
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "response"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10}
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        return mock_response
    
    settings = Settings(token='test')
    
    with patch('telegram_chat_bot.urlopen', side_effect=mock_urlopen):
        result = complete_with_usage(settings, [{"role": "user", "content": "hello"}])
    
    assert call_count == 2
    assert result.text == "response"


def test_complete_with_usage_raises_http_client_error_for_4xx():
    """Verify complete_with_usage raises HTTPClientError for 4xx errors."""
    call_count = [0]  # Use list for thread-safe counter
    
    def mock_urlopen(*args, **kwargs):
        call_count[0] += 1
        http_error = HTTPError("http://test", 400, "Bad Request", {}, None)
        raise http_error
    
    settings = Settings(token='test')
    
    with patch('telegram_chat_bot.urlopen', side_effect=mock_urlopen):
        with pytest.raises(HTTPClientError) as exc_info:
            asyncio.run(asyncio.to_thread(complete_with_usage, settings, [{"role": "user", "content": "test"}]))
    
    assert call_count[0] == 1
    assert "HTTP 400" in str(exc_info.value)


def test_handle_message_catches_http_client_error():
    """Verify handle_message catches HTTPClientError and pops history."""
    settings = Settings(token='test', allowed_user_ids={789})
    state = State(settings)
    user_id = 789
    state.history[user_id] = []
    state.successful_requests = 0
    state.failed_requests = 0
    state.request_semaphore = asyncio.Semaphore(1)
    state.usage = MagicMock()
    state.usage.reserve_request = MagicMock(return_value=(True, 100, 100))
    state.total_requests = 0
    state.user_request_count = {user_id: 0}
    state.total_response_time = 0.0
    
    mock_update = MagicMock()
    mock_update.message.content = "test message"
    mock_update.message.reply_text = AsyncMock()
    mock_update.effective_user.id = user_id
    mock_update.effective_chat.type = "private"
    
    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}
    
    # Patch complete_with_usage to raise HTTPClientError
    with patch('telegram_chat_bot.complete_with_usage', side_effect=HTTPClientError("HTTP 400: Bad Request")), \
         patch.object(state, 'persist', return_value=None):
        asyncio.run(handle_message(mock_update, mock_context))
    
    # History should be empty (user message popped as rollback)
    assert len(state.history[user_id]) == 0
    # Failed requests incremented
    assert state.failed_requests >= 1
    # Message sent
    assert mock_update.message.reply_text.called
