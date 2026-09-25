import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import URLError

import pytest

from telegram_chat_bot import (CompletionResult, State, Settings, UsageState, ProviderTimeoutError, complete,
                               complete_with_usage, private, _chunk, log_provider_failure,
                               get_provider_failures, _provider_failures,
                               _truncate_history_by_limit, _write_state_atomic,
                               handle_message)

@pytest.fixture(autouse=True)
def reset_circuit_breakers():
    """Reset circuit breaker state between all tests."""
    from telegram_chat_bot import _provider_circuit_breakers
    _provider_circuit_breakers.clear()
    yield
    _provider_circuit_breakers.clear()




def test_private_filter():
    # Test that private filter works correctly
    assert private() is not None


def test_settings_defaults():
    # Test default settings
    settings = Settings(token='test_token')
    assert settings.token == 'test_token'
    assert settings.base_url == 'http://127.0.0.1:8081/v1'
    assert settings.model == 'ministral-3-3b-64k-q4_k_m.gguf'
    assert settings.timeout == 120.0
    assert settings.max_messages == 20
    assert settings.max_chars == 8000
    assert settings.allowed_user_ids == frozenset()


def test_settings_custom():
    # Test custom settings
    with patch.dict('os.environ', {
        'LLAMA_CPP_BASE_URL': 'http://localhost:8080/v1',
        'LLAMA_CPP_MODEL': 'custom-model',
        'LLAMA_CPP_TIMEOUT': '300',
        'MAX_CONTEXT_MESSAGES': '10',
        'MAX_MESSAGE_CHARS': '4000'
        ,'TELEGRAM_ALLOWED_USER_IDS': '123, 456'
    }):
        settings = Settings(token='test_token')
        assert settings.base_url == 'http://localhost:8080/v1'
        assert settings.model == 'custom-model'
        assert settings.timeout == 300.0
        assert settings.max_messages == 10
        assert settings.max_chars == 4000
        assert settings.allowed_user_ids == frozenset({123, 456})


def test_state_initialization():
    # Test state initialization
    settings = Settings(token='test_token')
    state = State(settings)
    assert state.settings == settings
    assert isinstance(state.history, dict)
    assert state.global_prompt == ''
    assert isinstance(state.private_prompts, dict)
    assert not state.allowed(123)


def test_allowlist():
    with patch.dict('os.environ', {'TELEGRAM_ALLOWED_USER_IDS': '123'}):
        state = State(Settings(token='test_token'))
    assert state.allowed(123)
    assert not state.allowed(456)


def test_state_messages():
    # Test messages generation
    settings = Settings(token='test_token')
    state = State(settings)
    state.global_prompt = 'Global prompt'
    state.private_prompts[12345] = 'Private prompt'

    # Test with user ID that has no history
    messages = state.messages(12345)
    assert len(messages) == 1
    assert messages[0]['role'] == 'system'
    assert messages[0]['content'] == 'Global prompt\nPrivate prompt'

    # Test with user ID that has history
    state.history[12345].append({'role': 'user', 'content': 'Previous message'})
    messages = state.messages(12345)
    assert len(messages) == 2
    assert messages[1]['role'] == 'user'
    assert messages[1]['content'] == 'Previous message'


def test_state_messages_no_prompts():
    # Test messages with no prompts
    settings = Settings(token='test_token')
    state = State(settings)
    messages = state.messages(12345)
    assert len(messages) == 0


def test_state_messages_empty_history():
    # Test messages with empty history
    settings = Settings(token='test_token')
    state = State(settings)
    state.global_prompt = 'Global prompt'
    messages = state.messages(12345)
    assert len(messages) == 1
    assert messages[0]['role'] == 'system'
    assert messages[0]['content'] == 'Global prompt'


@patch('telegram_chat_bot.urlopen')
def test_complete_success(mock_urlopen):
    # Test successful completion
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        'choices': [{'message': {'content': 'Test response'}}]
    }).encode()
    mock_urlopen.return_value.__enter__.return_value = mock_response

    settings = Settings(token='test_token')
    messages = [{'role': 'user', 'content': 'Test message'}]
    result = complete(settings, messages)
    assert result == 'Test response'


@patch('telegram_chat_bot.urlopen')
def test_complete_empty_response(mock_urlopen):
    # Test empty response
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        'choices': [{'message': {'content': ''}}]
    }).encode()
    mock_urlopen.return_value.__enter__.return_value = mock_response

    settings = Settings(token='test_token')
    messages = [{'role': 'user', 'content': 'Test message'}]
    with pytest.raises(ProviderTimeoutError):
        complete(settings, messages)


@patch('telegram_chat_bot.urlopen')
def test_complete_http_error(mock_urlopen):
    # Test HTTP error
    mock_urlopen.side_effect = URLError('HTTP Error')

    settings = Settings(token='test_token')
    messages = [{'role': 'user', 'content': 'Test message'}]
    with pytest.raises(ProviderTimeoutError):
        complete(settings, messages)


@patch('telegram_chat_bot.urlopen')
def test_complete_json_error(mock_urlopen):
    # Test JSON decode error
    mock_response = MagicMock()
    mock_response.read.return_value = b'invalid json'
    mock_urlopen.return_value.__enter__.return_value = mock_response

    settings = Settings(token='test_token')
    messages = [{'role': 'user', 'content': 'Test message'}]
    with pytest.raises(ProviderTimeoutError):
        complete(settings, messages)


@patch('telegram_chat_bot.urlopen')
def test_complete_max_tokens_in_payload(mock_urlopen):
    # Verify max_tokens is included in the request payload
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        'choices': [{'message': {'content': 'ok'}}]
    }).encode()
    mock_urlopen.return_value.__enter__.return_value = mock_response

    settings = Settings(token='test_token')
    messages = [{'role': 'user', 'content': 'Test'}]
    result = complete(settings, messages)
    assert result == 'ok'

    # Check the payload was sent with max_tokens
    called_request = mock_urlopen.call_args[0][0]
    payload = json.loads(called_request.data)
    assert payload['max_tokens'] == settings.max_chars
    assert payload['model'] == settings.model
    assert payload['messages'] == messages
    assert payload['temperature'] == 0.7


@patch('telegram_chat_bot.urlopen')
def test_complete_response_truncation(mock_urlopen):
    # Verify response is truncated to max_chars
    settings = Settings(token='test_token')
    long_content = 'x' * (settings.max_chars + 100)
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        'choices': [{'message': {'content': long_content}}]
    }).encode()
    mock_urlopen.return_value.__enter__.return_value = mock_response

    messages = [{'role': 'user', 'content': 'Test'}]
    result = complete(settings, messages)
    assert len(result) == settings.max_chars
    assert result == 'x' * settings.max_chars


def test_admin_user_ids():
    with patch.dict('os.environ', {'TELEGRAM_ADMIN_USER_IDS': '123'}):
        state = State(Settings(token='test_token'))
    assert state.admin(123)
    assert not state.admin(456)
    assert not state.admin(12345)


def test_admin_empty():
    # Empty admin list means nobody is admin
    settings = Settings(token='test_token')
    state = State(settings)
    assert not state.admin(123)


def test_admin_vs_allowed():
    # An allowed user who is not admin cannot set global prompt
    with patch.dict('os.environ', {
        'TELEGRAM_ALLOWED_USER_IDS': '123',
        'TELEGRAM_ADMIN_USER_IDS': '456'
    }):
        settings = Settings(token='test_token')
        state = State(settings)
    assert state.allowed(123)
    assert not state.admin(123)
    assert not state.allowed(456)
    assert state.admin(456)


def test_chunk_empty():
    assert _chunk('') == ['']


def test_chunk_single():
    text = 'a' * 100
    assert _chunk(text) == [text]


def test_chunk_exact_boundary():
    text = 'a' * 4096
    assert _chunk(text) == [text]


def test_chunk_over_boundary():
    text = 'a' * 4097
    result = _chunk(text)
    assert len(result) == 2
    assert len(result[0]) == 4096
    assert len(result[1]) == 1


def test_chunk_multi_boundary():
    text = 'a' * 8200
    result = _chunk(text)
    assert len(result) == 3
    assert len(result[0]) == 4096
    assert len(result[1]) == 4096
    assert len(result[2]) == 8


def test_chunk_custom_max():
    result = _chunk('abcde', 3)
    assert result == ['abc', 'de']


def test_chunk_preserves_full_response():
    """Regression: _chunk(response[:4096]) truncated before chunking.
    The full response from complete() must be split into Telegram-safe chunks."""
    full = 'a' * 8192  # 2 full chunks + 0 extra (8192 = 2*4096)
    chunks = _chunk(full)
    assert len(chunks) == 2
    assert len(chunks[0]) == 4096
    assert len(chunks[1]) == 4096
    assert ''.join(chunks) == full  # full response preserved


def test_chunk_preserves_over_boundary_full():
    """Full response > 4096 must not lose trailing chars."""
    full = 'b' * 6000  # 2 chunks: 4096 + 1904
    chunks = _chunk(full)
    assert len(chunks) == 2
    assert len(chunks[0]) == 4096
    assert len(chunks[1]) == 1904
    assert ''.join(chunks) == full


def test_chunk_single_chunk_preserved():
    """Single-chunk response must be unchanged."""
    text = 'c' * 2000
    chunks = _chunk(text)
    assert chunks == [text]


# ---- Provider resilience tests ----


def test_log_provider_failure():
    """Test that provider failures are logged without exposing URLs or credentials."""
    # Clear any existing failures
    _provider_failures.clear()

    log_provider_failure("llama.cpp", "timeout")
    failures = get_provider_failures()

    assert len(failures) == 1
    assert failures[0].provider == "llama.cpp"
    assert failures[0].error_type == "timeout"
    # Should not contain any URL or credential information
    assert "http" not in str(failures[0])
    assert "token" not in str(failures[0]).lower()


def test_get_provider_failures_isolation():
    """Test that get_provider_failures returns a copy, not the original list."""
    _provider_failures.clear()
    log_provider_failure("test", "error1")

    failures1 = get_provider_failures()
    failures2 = get_provider_failures()

    assert failures1 is not failures2
    assert failures1 == failures2


def test_complete_independent_timeout():
    """Test that complete() has an independent timeout mechanism.

    The implementation uses ThreadPoolExecutor with a timeout parameter
    to ensure a stalled provider cannot block the request indefinitely.
    """
    import time
    from unittest.mock import patch, MagicMock

    # Create a settings object with a very short timeout and fewer retries
    settings = Settings(token="test", timeout=0.1, max_retries=1)

    mock_response = MagicMock()

    def slow_request():
        time.sleep(10)  # Simulate a very slow/stalled request
        return b'{"choices": [{"message": {"content": "ok"}}]}'

    mock_response.read.side_effect = slow_request

    with patch('telegram_chat_bot.urlopen') as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # The request should timeout, not hang
        start = time.time()
        try:
            complete(settings, [{"role": "user", "content": "test"}])
            assert False, "Expected RuntimeError"
        except RuntimeError:
            elapsed = time.time() - start
            # Should have timed out, not waited for the full 10 seconds
            assert elapsed < 3, f"Request took {elapsed}s, expected timeout"


@pytest.mark.asyncio
async def test_handle_message_rejects_unallowlisted_user():
    """handle_message must send onboarding message when user is not allowlisted."""
    from telegram_chat_bot import handle_message, State

    # No env set → allowed_user_ids is empty → everyone rejected
    settings = Settings(token='test_token')
    state = State(settings)

    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 999
    mock_update.message.text = "hello"
    mock_update.message.reply_text = AsyncMock()

    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}

    await handle_message(mock_update, mock_context)

    # Onboarding message should be sent
    mock_update.message.reply_text.assert_called_once()
    call_args = mock_update.message.reply_text.call_args[0][0]
    assert "access" in call_args.lower()


@pytest.mark.asyncio
async def test_handle_message_ignores_non_private_chat():
    """handle_message must ignore group/supergroup/channel messages entirely."""
    from telegram_chat_bot import handle_message, State

    settings = Settings(token='test_token')
    state = State(settings)

    mock_update = MagicMock()
    mock_update.effective_chat.type = "group"
    mock_update.effective_user.id = 123
    mock_update.message.text = "hello"
    mock_update.message.reply_text = MagicMock()

    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}

    await handle_message(mock_update, mock_context)

    # No reply should have been sent
    mock_update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_rate_limiting():
    """A second request within the interval gets the wait reply and does NOT call
    the model or add history."""
    settings = Settings(token='test', allowed_user_ids=frozenset({123}), request_interval=5.0)
    state = State(settings)
    state.last_request[123] = time.monotonic()

    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 123
    mock_update.message.text = "hello"
    mock_update.message.reply_text = AsyncMock()

    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}

    await handle_message(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once_with(
        "Please wait a moment before sending another message."
    )
    assert len(state.history[123]) == 0


@pytest.mark.asyncio
async def test_handle_message_model_failure_rollback():
    """When the model raises, the user message is removed from history and an
    error reply is sent."""
    settings = Settings(token='test', allowed_user_ids=frozenset({123}))
    state = State(settings)
    state.last_request[123] = 0

    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 123
    mock_update.message.text = "hello"
    mock_update.message.reply_text = AsyncMock()

    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}

    with patch("telegram_chat_bot.complete_with_usage", side_effect=RuntimeError("model request failed")):
        await handle_message(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    call_args = mock_update.message.reply_text.call_args[0][0]
    assert "failed" in call_args.lower()
    assert "try again shortly" in call_args.lower()
    assert "admin" not in call_args.lower()  # No false admin notification claim
    assert len(state.history[123]) == 0


# --- health_check tests ---


def test_health_check_healthy():
    """Healthy /health returns (model_name, True)."""
    from telegram_chat_bot import health_check
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"model": "ministral-3-3b-64k-q4_k_m.gguf"}).encode()
    with patch("telegram_chat_bot.urlopen", return_value=MagicMock(
            __enter__=MagicMock(return_value=mock_response),
            __exit__=MagicMock(return_value=False))):
        model_name, is_online = health_check("http://127.0.0.1:11438/v1", timeout=5)
    assert is_online is True
    assert model_name == "ministral-3-3b-64k-q4_k_m.gguf"


def test_health_check_unreachable():
    """Unreachable /health returns ("unreachable", False)."""
    from telegram_chat_bot import health_check
    with patch("telegram_chat_bot.urlopen", side_effect=URLError("connection refused")):
        model_name, is_online = health_check("http://127.0.0.1:9999/v1", timeout=5)
    assert is_online is False
    assert model_name == "unreachable"


def test_health_check_strips_v1_suffix():
    """Health URL must be {base}/health, not {base}/v1/health."""
    from telegram_chat_bot import health_check
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"model": "test-model"}).encode()
    mock_ctx = MagicMock(__enter__=MagicMock(return_value=mock_response),
                         __exit__=MagicMock(return_value=False))
    with patch("telegram_chat_bot.urlopen", return_value=mock_ctx) as mock_urlopen:
        health_check("http://127.0.0.1:11438/v1", timeout=5)
    expected_url = "http://127.0.0.1:11438/health"
    call_url = mock_urlopen.call_args[0][0].full_url
    assert call_url == expected_url


def test_health_check_trailing_slash():
    """Trailing slash in base_url must not produce /health/health."""
    from telegram_chat_bot import health_check
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"model": "test"}).encode()
    mock_ctx = MagicMock(__enter__=MagicMock(return_value=mock_response),
                         __exit__=MagicMock(return_value=False))
    with patch("telegram_chat_bot.urlopen", return_value=mock_ctx) as mock_urlopen:
        health_check("http://127.0.0.1:11438/v1/", timeout=5)
    call_url = mock_urlopen.call_args[0][0].full_url
    assert call_url == "http://127.0.0.1:11438/health"


def test_health_check_no_secrets():
    """health_check must not expose secrets (only model name and boolean)."""
    from telegram_chat_bot import health_check
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "model": "secret-model",
        "secret_key": "abc123"
    }).encode()
    mock_ctx = MagicMock(__enter__=MagicMock(return_value=mock_response),
                         __exit__=MagicMock(return_value=False))
    with patch("telegram_chat_bot.urlopen", return_value=mock_ctx):
        model_name, is_online = health_check("http://127.0.0.1:11438/v1", timeout=5)
    assert is_online is True
    assert model_name == "secret-model"
    # No secret_key should leak
    assert "secret_key" not in repr((model_name, is_online))


def test_health_check_path_without_v1_suffix():
    """Regression: base URL with a path that does NOT end in /v1 must not be mutated.

    E.g. a base_url like "http://127.0.0.1:11438/api" should produce
    "http://127.0.0.1:11438/api/health", not "http://127.0.0.1/health".
    """
    from telegram_chat_bot import health_check

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"model": "test-model"}).encode()
    mock_ctx = MagicMock(__enter__=MagicMock(return_value=mock_response),
                         __exit__=MagicMock(return_value=False))
    with patch("telegram_chat_bot.urlopen", return_value=mock_ctx) as mock_urlopen:
        health_check("http://127.0.0.1:11438/api", timeout=5)

    call_url = mock_urlopen.call_args[0][0].full_url
    assert call_url == "http://127.0.0.1:11438/api/health"


def test_health_check_v1alpha_suffix():
    """A /v1alpha suffix must NOT be stripped — it should keep /api/v1alpha/health."""
    from telegram_chat_bot import health_check

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"model": "test-model"}).encode()
    mock_ctx = MagicMock(__enter__=MagicMock(return_value=mock_response),
                         __exit__=MagicMock(return_value=False))
    with patch("telegram_chat_bot.urlopen", return_value=mock_ctx) as mock_urlopen:
        health_check("http://host/api/v1alpha", timeout=5)

    call_url = mock_urlopen.call_args[0][0].full_url
    assert call_url == "http://host/api/v1alpha/health"


# ---- Numeric setting validation tests ----


def test_validate_numeric_setting_imports():
    """Confirm _validate_numeric_setting is importable."""
    from telegram_chat_bot import _validate_numeric_setting
    assert callable(_validate_numeric_setting)


def test_validate_numeric_setting_positive_int():
    from telegram_chat_bot import _validate_numeric_setting
    assert _validate_numeric_setting("42", "TEST") == 42
    assert _validate_numeric_setting("1", "TEST") == 1


def test_validate_numeric_setting_positive_float():
    from telegram_chat_bot import _validate_numeric_setting
    assert _validate_numeric_setting("1.5", "TEST", as_float=True) == 1.5
    assert _validate_numeric_setting("0.1", "TEST", as_float=True) == 0.1


def test_validate_numeric_setting_non_numeric_raises():
    from telegram_chat_bot import _validate_numeric_setting
    for bad in ("abc", "", "1.2.3", "  "):
        with pytest.raises(ValueError) as exc:
            _validate_numeric_setting(bad, "TEST")
        assert "must be a numeric value" in str(exc.value)


def test_validate_numeric_setting_zero_raises():
    from telegram_chat_bot import _validate_numeric_setting
    with pytest.raises(ValueError) as exc:
        _validate_numeric_setting("0", "TEST")
    assert "must be positive" in str(exc.value)


def test_validate_numeric_setting_negative_raises():
    from telegram_chat_bot import _validate_numeric_setting
    with pytest.raises(ValueError) as exc:
        _validate_numeric_setting("-5", "TEST")
    assert "must be positive" in str(exc.value)


def test_validate_numeric_setting_negative_float_raises():
    from telegram_chat_bot import _validate_numeric_setting
    with pytest.raises(ValueError) as exc:
        _validate_numeric_setting("-0.5", "TEST", as_float=True)
    assert "must be positive" in str(exc.value)


def test_validate_numeric_setting_none_raises():
    from telegram_chat_bot import _validate_numeric_setting
    with pytest.raises(ValueError) as exc:
        _validate_numeric_setting(None, "TEST")
    assert "must be a numeric value" in str(exc.value)


# ---- Invalid environment values via Settings ----


def test_settings_invalid_timeout_raises():
    with patch.dict('os.environ', {
        'LLAMA_CPP_TIMEOUT': 'abc',
    }):
        with pytest.raises(ValueError) as exc:
            Settings(token='test_token')
        assert "LLAMA_CPP_TIMEOUT" in str(exc.value)


def test_settings_invalid_max_context_raises():
    with patch.dict('os.environ', {
        'MAX_CONTEXT_MESSAGES': '-1',
    }):
        with pytest.raises(ValueError) as exc:
            Settings(token='test_token')
        assert "MAX_CONTEXT_MESSAGES" in str(exc.value)


def test_settings_invalid_max_chars_raises():
    with patch.dict('os.environ', {
        'MAX_MESSAGE_CHARS': '0',
    }):
        with pytest.raises(ValueError) as exc:
            Settings(token='test_token')
        assert "MAX_MESSAGE_CHARS" in str(exc.value)


def test_settings_invalid_request_interval_raises():
    with patch.dict('os.environ', {
        'MODEL_REQUEST_INTERVAL': 'xyz',
    }):
        with pytest.raises(ValueError) as exc:
            Settings(token='test_token')
        assert "MODEL_REQUEST_INTERVAL" in str(exc.value)


def test_settings_zero_timeout_raises():
    with patch.dict('os.environ', {
        'LLAMA_CPP_TIMEOUT': '0',
    }):
        with pytest.raises(ValueError) as exc:
            Settings(token='test_token')
        assert "must be positive" in str(exc.value)


def test_settings_default_validations():
    """Defaults (unset vars) must parse to valid numeric values."""
    settings = Settings(token='test_token')
    assert settings.timeout == 120.0
    assert settings.max_messages == 20
    assert settings.max_chars == 8000
    assert settings.request_interval == 1.0


def test_settings_valid_float_interval():
    with patch.dict('os.environ', {
        'MODEL_REQUEST_INTERVAL': '2.5',
    }):
        settings = Settings(token='test_token')
    assert settings.request_interval == 2.5


def test_settings_valid_int_values():
    with patch.dict('os.environ', {
        'LLAMA_CPP_TIMEOUT': '60',
        'MAX_CONTEXT_MESSAGES': '50',
        'MAX_MESSAGE_CHARS': '16000',
    }):
        settings = Settings(token='test_token')
    assert settings.timeout == 60.0
    assert settings.max_messages == 50
    assert settings.max_chars == 16000


# ---- NaN / Inf regression tests ----


def test_validate_numeric_setting_nan_raises():
    """nan must be rejected with a descriptive ValueError."""
    from telegram_chat_bot import _validate_numeric_setting
    with pytest.raises(ValueError) as exc:
        _validate_numeric_setting("nan", "TEST", as_float=True)
    assert "finite positive value" in str(exc.value)


def test_validate_numeric_setting_inf_raises():
    """inf must be rejected with a descriptive ValueError."""
    from telegram_chat_bot import _validate_numeric_setting
    with pytest.raises(ValueError) as exc:
        _validate_numeric_setting("inf", "TEST", as_float=True)
    assert "finite positive value" in str(exc.value)


def test_validate_numeric_setting_negative_inf_raises():
    """-inf must be rejected with a descriptive ValueError.

    -inf is <= 0 so gets caught by the positive check first.
    """
    from telegram_chat_bot import _validate_numeric_setting
    with pytest.raises(ValueError) as exc:
        _validate_numeric_setting("-inf", "TEST", as_float=True)
    # -inf is <= 0, so the "must be positive" path fires
    assert "must be positive" in str(exc.value)


def test_validate_numeric_setting_infinity_raises():
    """'Infinity' (Python float alias for inf) must be rejected."""
    from telegram_chat_bot import _validate_numeric_setting
    with pytest.raises(ValueError) as exc:
        _validate_numeric_setting("Infinity", "TEST", as_float=True)
    assert "finite positive value" in str(exc.value)


def test_validate_numeric_setting_negative_infinity_raises():
    """'-Infinity' must be rejected.

    Like -inf, -Infinity <= 0 so the positive check catches it.
    """
    from telegram_chat_bot import _validate_numeric_setting
    with pytest.raises(ValueError) as exc:
        _validate_numeric_setting("-Infinity", "TEST", as_float=True)
    # -Infinity is <= 0, so the "must be positive" path fires
    assert "must be positive" in str(exc.value)


def test_settings_invalid_nan_timeout_raises():
    """LLAMA_CPP_TIMEOUT=nan must raise ValueError at Settings init.

    LLAMA_CPP_TIMEOUT is validated as int, so "nan" is caught by the
    non-numeric path (int("nan") raises ValueError).
    """
    with patch.dict("os.environ", {"LLAMA_CPP_TIMEOUT": "nan"}):
        with pytest.raises(ValueError) as exc:
            Settings(token="test_token")
        assert "LLAMA_CPP_TIMEOUT" in str(exc.value)
        assert "must be a numeric value" in str(exc.value)


def test_settings_invalid_inf_interval_raises():
    """MODEL_REQUEST_INTERVAL=inf must raise ValueError at Settings init."""
    with patch.dict("os.environ", {"MODEL_REQUEST_INTERVAL": "inf"}):
        with pytest.raises(ValueError) as exc:
            Settings(token="test_token")
        assert "MODEL_REQUEST_INTERVAL" in str(exc.value)
        assert "finite positive value" in str(exc.value)


def test_settings_invalid_nan_interval_raises():
    """MODEL_REQUEST_INTERVAL=nan must raise ValueError (as_float=True path)."""
    with patch.dict("os.environ", {"MODEL_REQUEST_INTERVAL": "nan"}):
        with pytest.raises(ValueError) as exc:
            Settings(token="test_token")
        assert "MODEL_REQUEST_INTERVAL" in str(exc.value)
        assert "finite positive value" in str(exc.value)


def test_settings_invalid_negative_inf_interval_raises():
    """MODEL_REQUEST_INTERVAL=-inf must raise ValueError (as_float=True path).

    -inf is <= 0, so the "must be positive" check catches it.
    """
    with patch.dict("os.environ", {"MODEL_REQUEST_INTERVAL": "-inf"}):
        with pytest.raises(ValueError) as exc:
            Settings(token="test_token")
        assert "MODEL_REQUEST_INTERVAL" in str(exc.value)
        assert "must be positive" in str(exc.value)


def test_registration_store_persists_status(tmp_path):
    from telegram_chat_bot import RegistrationStore

    path = tmp_path / "registrations.json"
    store = RegistrationStore(str(path))
    store.set_status(123, "pending")
    assert store.status(123) == "pending"

    restored = RegistrationStore(str(path))
    assert restored.status(123) == "pending"
    assert restored.users("pending") == [123]


@pytest.mark.asyncio
async def test_start_creates_pending_registration(tmp_path):
    from telegram_chat_bot import Settings, State, start

    settings = Settings(token="test", registration_file=str(tmp_path / "registrations.json"))
    state = State(settings)
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.application.bot_data = {"state": state}

    await start(update, context)

    assert state.user_registrations.status(123) == "pending"
    update.message.reply_text.assert_awaited_once_with(
        "Your access request was recorded and is pending administrator approval."
    )


@pytest.mark.asyncio
async def test_approved_registration_is_allowed(tmp_path):
    from telegram_chat_bot import Settings, State

    settings = Settings(token="test", registration_file=str(tmp_path / "registrations.json"))
    state = State(settings)
    state.user_registrations.set_status(123, "approved")
    assert state.allowed(123)


@pytest.mark.asyncio
async def test_non_admin_cannot_approve_registration(tmp_path):
    from telegram_chat_bot import Settings, State, approve

    settings = Settings(token="test", registration_file=str(tmp_path / "registrations.json"))
    state = State(settings)
    update = MagicMock()
    update.effective_user.id = 999
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["123"]
    context.application.bot_data = {"state": state}

    await approve(update, context)

    assert state.user_registrations.status(123) is None
    update.message.reply_text.assert_awaited_once_with("Only administrators can approve users.")


@pytest.mark.asyncio
async def test_start_repeat_registration_is_pending(tmp_path):
    from telegram_chat_bot import Settings, State, start

    settings = Settings(token="test", registration_file=str(tmp_path / "registrations.json"))
    state = State(settings)
    state.user_registrations.set_status(123, "pending")
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.application.bot_data = {"state": state}

    await start(update, context)

    update.message.reply_text.assert_awaited_once_with(
        "Your access request is still pending administrator approval."
    )


@pytest.mark.asyncio
async def test_admin_can_approve_and_reject_registration(tmp_path):
    from telegram_chat_bot import Settings, State, approve, reject

    settings = Settings(token="test", admin_user_ids=frozenset({999}), registration_file=str(tmp_path / "registrations.json"))
    state = State(settings)
    state.user_registrations.set_status(123, "pending")
    update = MagicMock()
    update.effective_user.id = 999
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["123"]
    context.application.bot_data = {"state": state}

    await approve(update, context)
    assert state.user_registrations.status(123) == "approved"
    await reject(update, context)
    assert state.user_registrations.status(123) == "rejected"


@pytest.mark.asyncio
async def test_rejected_registration_cannot_start_or_chat(tmp_path):
    from telegram_chat_bot import Settings, State, handle_message, start

    settings = Settings(token="test", registration_file=str(tmp_path / "registrations.json"))
    state = State(settings)
    state.user_registrations.set_status(123, "rejected")
    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_user.id = 123
    update.message.text = "hello"
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.application.bot_data = {"state": state}

    await start(update, context)
    await handle_message(update, context)

    assert state.user_registrations.status(123) == "rejected"
    assert list(state.history[123]) == []
    # Two replies: /start rejection + handle_message rejection
    assert update.message.reply_text.call_count == 2
    # First call: /start rejection
    call1 = update.message.reply_text.call_args_list[0][0][0]
    assert "rejected" in call1.lower()
    # Second call: handle_message rejection (same message for simplicity)
    call2 = update.message.reply_text.call_args_list[1][0][0]
    assert "rejected" in call2.lower()


@pytest.mark.asyncio
async def test_admin_can_list_and_revoke_users(tmp_path):
    from telegram_chat_bot import Settings, State, revoke, users

    settings = Settings(token="test", admin_user_ids=frozenset({999}), registration_file=str(tmp_path / "registrations.json"))
    state = State(settings)
    state.user_registrations.set_status(123, "pending")
    state.user_registrations.set_status(456, "approved")
    update = MagicMock()
    update.effective_user.id = 999
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = []
    context.application.bot_data = {"state": state}

    await users(update, context)
    update.message.reply_text.assert_awaited_once_with("Pending: 123\nApproved: 456")
    context.args = ["456"]
    await revoke(update, context)
    assert state.user_registrations.status(456) == "rejected"
    update.message.reply_text.assert_awaited_with("User 456 revoked.")


@pytest.mark.asyncio
async def test_start_parameter_is_rejected_without_registration(tmp_path):
    from telegram_chat_bot import Settings, State, start

    settings = Settings(token="test", registration_file=str(tmp_path / "registrations.json"))
    state = State(settings)
    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["invite-code"]
    context.application.bot_data = {"state": state}

    await start(update, context)

    assert state.user_registrations.status(123) is None
    update.message.reply_text.assert_awaited_once_with(
        "This bot does not support /start parameters. Send /start without additional text to request access."
    )
# --- Persistent state tests ---


class TestPersistentState:
    """Tests for issue #11: persistent conversation state."""

    def test_settings_state_file_default(self):
        """Default state_file follows the XDG user-state location."""
        with patch("os.path.expanduser", return_value="/home/test/.local/state/telegram-chat-bot/state.json"):
            settings = Settings(token='test')
        assert settings.state_file == "/home/test/.local/state/telegram-chat-bot/state.json"

    def test_settings_custom_state_file(self):
        """Custom state_file can be set via environment variable."""
        with patch.dict('os.environ', {'TELEGRAM_CHAT_BOT_STATE_FILE': '/custom/path/state.json'}):
            settings = Settings(token='test')
        assert settings.state_file == '/custom/path/state.json'

    def test_settings_max_history_default(self):
        """Default max_history is 100."""
        settings = Settings(token='test')
        assert settings.max_history == 100

    def test_settings_custom_max_history(self):
        """Custom max_history can be set via environment variable."""
        with patch.dict('os.environ', {'TELEGRAM_CHAT_BOT_MAX_HISTORY': '50'}):
            settings = Settings(token='test')
        assert settings.max_history == 50

    def test_settings_invalid_max_history_raises(self):
        """Invalid max_history must raise ValueError."""
        with patch.dict('os.environ', {'TELEGRAM_CHAT_BOT_MAX_HISTORY': 'abc'}):
            with pytest.raises(ValueError) as exc:
                Settings(token='test')
        assert "TELEGRAM_CHAT_BOT_MAX_HISTORY" in str(exc.value)

    def test_settings_zero_max_history_raises(self):
        """Zero max_history must raise ValueError."""
        with patch.dict('os.environ', {'TELEGRAM_CHAT_BOT_MAX_HISTORY': '0'}):
            with pytest.raises(ValueError) as exc:
                Settings(token='test')
        assert "must be positive" in str(exc.value)

    def test_truncate_history_within_limit(self):
        """History within limit is unchanged."""
        history = [{'role': 'user', 'content': 'msg1'}, {'role': 'assistant', 'content': 'msg2'}]
        result = _truncate_history_by_limit(history, 100)
        assert result == history

    def test_truncate_history_at_limit(self):
        """History at limit is unchanged."""
        history = [{'role': 'user', 'content': f'msg{i}'} for i in range(10)]
        result = _truncate_history_by_limit(history, 10)
        assert result == history

    def test_truncate_history_over_limit(self):
        """History over limit is truncated, keeping oldest entries."""
        history = [{'role': 'user', 'content': f'msg{i}'} for i in range(20)]
        result = _truncate_history_by_limit(history, 10)
        assert len(result) == 10
        # Slice from back gives us msgs 10-19
        for i in range(10):
            assert result[i]['content'] == f'msg{10 + i}'

    @pytest.mark.asyncio
    async def test_state_load_nonexistent_file(self):
        """State load from nonexistent file returns empty state."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            f.write(b'')
            temp_path = f.name
        try:
            settings = Settings(token='test', state_file=temp_path)
            state = State(settings)
            state._load()
            assert state.global_prompt == ''
            assert state.private_prompts == {}
            assert state.history == {}
            assert state.last_request == {}
        finally:
            os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_state_load_invalid_json(self):
        """State load from invalid JSON returns empty state."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
            f.write('not valid json')
            temp_path = f.name
        try:
            settings = Settings(token='test', state_file=temp_path)
            state = State(settings)
            state._load()
            assert state.global_prompt == ''
            assert state.private_prompts == {}
            assert state.history == {}
            assert state.last_request == {}
        finally:
            os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_state_load_valid_json(self):
        """State load from valid JSON restores state."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
            json.dump({
                "global_prompt": "Test global",
                "private_prompts": {"123": "Private for 123", "456": "Private for 456"},
                "user_history": {"123": [{"role": "user", "content": "Hi"}]},
                "last_request": {"123": 12345.0}
            }, f)
            temp_path = f.name
        try:
            settings = Settings(token='test', state_file=temp_path)
            state = State(settings)
            state._load()
            assert state.global_prompt == "Test global"
            assert state.private_prompts[123] == "Private for 123"
            assert state.private_prompts[456] == "Private for 456"
            assert len(state.history[123]) == 1
            assert state.history[123][0]['content'] == "Hi"
            assert state.last_request[123] == 12345.0
        finally:
            os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_state_load_truncates_history(self):
        """State load truncates history to max_history limit, keeping oldest entries."""
        history = [{"role": "user", "content": f"msg{i}"} for i in range(200)]
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
            json.dump({
                "user_history": {"123": history}
            }, f)
            temp_path = f.name
        try:
            settings = Settings(token='test', state_file=temp_path, max_history=50)
            state = State(settings)
            state._load()
            # deque(maxlen=20) means it keeps latest 20 from the truncated list
            assert len(state.history[123]) == 20
            # Should have kept oldest 50 from 200, then deque keeps newest 20 of those
            # Oldest 50 from 200 are msgs 150-199
            # Newest 20 of those are msgs 180-199
            assert state.history[123][0]['content'] == "msg180"
            assert state.history[123][19]['content'] == "msg199"
        finally:
            os.unlink(temp_path)

    def test_write_state_atomic_creates_dir(self):
        """_write_state_atomic creates parent directories."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "subdir", "state.json")
            _write_state_atomic(state_file, {"test": "value"})
            assert os.path.exists(state_file)

    def test_write_state_atomic_sets_permissions(self):
        """_write_state_atomic sets restrictive permissions (0o600)."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            temp_path = f.name
        try:
            os.unlink(temp_path)
            _write_state_atomic(temp_path, {"test": "value"})
            mode = os.stat(temp_path).st_mode & 0o777
            assert mode == 0o600
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_write_state_atomic_atomic(self):
        """_write_state_atomic uses atomic rename."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            temp_path = f.name
        try:
            os.unlink(temp_path)
            _write_state_atomic(temp_path, {"step1": True})
            # Read and verify
            with open(temp_path, 'r') as f:
                data = json.load(f)
            assert data == {"step1": True}
            # Write again
            _write_state_atomic(temp_path, {"step2": True})
            with open(temp_path, 'r') as f:
                data = json.load(f)
            assert data == {"step2": True}
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_state_persist(self):
        """State persist writes state to file."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            temp_path = f.name
        try:
            os.unlink(temp_path)
            settings = Settings(token='test', state_file=temp_path)
            state = State(settings)
            state.global_prompt = "Test prompt"
            state.private_prompts[123] = "Private"
            state.history[123].append({"role": "user", "content": "Hi"})
            await state.persist()
            # Verify file was written
            assert os.path.exists(temp_path)
            with open(temp_path, 'r') as f:
                data = json.load(f)
            assert data["global_prompt"] == "Test prompt"
            assert data["private_prompts"]["123"] == "Private"
            assert len(data["user_history"]["123"]) == 1
            # Verify permissions
            mode = os.stat(temp_path).st_mode & 0o777
            assert mode == 0o600
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_state_reset(self):
        """State reset clears user history and last_request, and persists."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            temp_path = f.name
        try:
            os.unlink(temp_path)
            settings = Settings(token='test', state_file=temp_path)
            state = State(settings)
            state.history[123].append({"role": "user", "content": "Hi"})
            state.last_request[123] = 12345.0
            await state.persist()
            # Verify persisted
            with open(temp_path, 'r') as f:
                data = json.load(f)
            assert "123" in data["user_history"]
            assert "123" in data["last_request"]
            # Now reset
            await state.reset(123)
            # Verify cleared
            assert 123 not in state.history
            assert 123 not in state.last_request
            # Verify persisted
            with open(temp_path, 'r') as f:
                data = json.load(f)
            assert "123" not in data["user_history"]
            assert "123" not in data["last_request"]
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


    def test_state_load_migrates_legacy_path(self, tmp_path):
        """Legacy state is read when the XDG state file is absent."""
        legacy_path = tmp_path / "legacy" / "state.json"
        state_path = tmp_path / "xdg" / "state.json"
        legacy_path.parent.mkdir()
        legacy_path.write_text(json.dumps({"global_prompt": "legacy prompt"}))
        settings = Settings(token="test", state_file=str(state_path), legacy_state_file=str(legacy_path))
        state = State(settings)
        state._load()
        assert state.global_prompt == "legacy prompt"
        state._dump()
        assert state_path.exists()
        assert legacy_path.exists()


# ---- Numeric setting validation tests for new settings ----


def test_settings_invalid_state_file_max_history_raises():
    """Invalid TELEGRAM_CHAT_BOT_MAX_HISTORY must raise ValueError."""
    with patch.dict('os.environ', {
        'TELEGRAM_CHAT_BOT_MAX_HISTORY': 'abc',
    }):
        with pytest.raises(ValueError) as exc:
            Settings(token='test_token')
        assert "TELEGRAM_CHAT_BOT_MAX_HISTORY" in str(exc.value)


def test_settings_zero_state_file_max_history_raises():
    """Zero TELEGRAM_CHAT_BOT_MAX_HISTORY must raise ValueError."""
    with patch.dict('os.environ', {
        'TELEGRAM_CHAT_BOT_MAX_HISTORY': '0',
    }):
        with pytest.raises(ValueError) as exc:
            Settings(token='test_token')
        assert "must be positive" in str(exc.value)


def test_settings_valid_max_history():
    """Valid TELEGRAM_CHAT_BOT_MAX_HISTORY is accepted."""
    with patch.dict('os.environ', {
        'TELEGRAM_CHAT_BOT_MAX_HISTORY': '200',
    }):
        settings = Settings(token='test_token')
    assert settings.max_history == 200


def test_settings_negative_max_history_raises():
    """Negative TELEGRAM_CHAT_BOT_MAX_HISTORY must raise ValueError."""
    with patch.dict('os.environ', {
        'TELEGRAM_CHAT_BOT_MAX_HISTORY': '-5',
    }):
        with pytest.raises(ValueError) as exc:
            Settings(token='test_token')
        assert "must be positive" in str(exc.value)


def test_openrouter_account_limit_is_shared_across_users():
    settings = Settings(token="test", openrouter_enabled=True, default_daily_request_limit=0)
    usage = UsageState(settings, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())

    for request_number in range(49):
        assert usage.reserve_request(100 if request_number % 2 else 200, "openrouter")[0] is True

    assert usage.reserve_request(100, "openrouter")[0] is True
    allowed, limit, remaining = usage.reserve_request(200, "openrouter")
    assert (allowed, limit, remaining) == (False, 50, 0)
    assert usage.stats(100, "openrouter")["account_requests"] == 50


def test_usage_stats_reports_effective_minimum_of_user_and_account_limits():
    settings = Settings(
        token="test",
        openrouter_enabled=True,
        default_daily_request_limit=10,
        openrouter_daily_request_limit=50,
    )
    usage = UsageState(settings, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())

    stats = usage.stats(100, "openrouter")

    assert stats["limit"] == 10
    assert stats["account_limit"] == 50
    assert stats["remaining"] == 10


def test_usage_state_records_tokens_and_persists_with_registration_state(tmp_path):
    state_file = tmp_path / "state.json"
    registration_file = tmp_path / "registrations.json"
    settings = Settings(token="test", state_file=str(state_file), registration_file=str(registration_file))
    state = State(settings)
    state.usage.reserve_request(123, "llama.cpp")
    state.usage.record_tokens(123, "llama.cpp", 12, 34)
    asyncio.run(state.persist())

    restored = State(settings)
    restored._load()
    stats = restored.usage.stats(123, "llama.cpp")
    assert stats["request_count"] == 1
    assert stats["total_tokens"] == 46


@patch("telegram_chat_bot.urlopen")
def test_complete_with_usage_reads_provider_token_counts(mock_urlopen):
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 5},
    }).encode()
    mock_urlopen.return_value.__enter__.return_value = mock_response

    result = complete_with_usage(Settings(token="test"), [{"role": "user", "content": "hi"}])
    assert result == CompletionResult("ok", 7, 5)
    assert result.total_tokens == 12
