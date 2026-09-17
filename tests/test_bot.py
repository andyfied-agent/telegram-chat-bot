import json
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from telegram_chat_bot import State, Settings, complete, private, _chunk


def test_private_filter():
    # Test that private filter works correctly
    assert private() is not None


def test_settings_defaults():
    # Test default settings
    settings = Settings(token='test_token')
    assert settings.token == 'test_token'
    assert settings.base_url == 'http://127.0.0.1:11438/v1'
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
    with pytest.raises(RuntimeError, match='model request failed'):
        complete(settings, messages)


@patch('telegram_chat_bot.urlopen')
def test_complete_http_error(mock_urlopen):
    # Test HTTP error
    mock_urlopen.side_effect = URLError('HTTP Error')

    settings = Settings(token='test_token')
    messages = [{'role': 'user', 'content': 'Test message'}]
    with pytest.raises(RuntimeError, match='model request failed'):
        complete(settings, messages)


@patch('telegram_chat_bot.urlopen')
def test_complete_json_error(mock_urlopen):
    # Test JSON decode error
    mock_response = MagicMock()
    mock_response.read.return_value = b'invalid json'
    mock_urlopen.return_value.__enter__.return_value = mock_response

    settings = Settings(token='test_token')
    messages = [{'role': 'user', 'content': 'Test message'}]
    with pytest.raises(RuntimeError, match='model request failed'):
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


@pytest.mark.asyncio
async def test_handle_message_rejects_unallowlisted_user():
    """handle_message must return early without any reply when user is not allowlisted."""
    from telegram_chat_bot import handle_message

    # No env set → allowed_user_ids is empty → everyone rejected
    settings = Settings(token='test_token')
    state = State(settings)

    mock_update = MagicMock()
    mock_update.effective_chat.type = "private"
    mock_update.effective_user.id = 999
    mock_update.message.text = "hello"
    mock_update.message.reply_text = MagicMock()

    mock_context = MagicMock()
    mock_context.application.bot_data = {"state": state}

    await handle_message(mock_update, mock_context)

    # No reply should have been sent
    mock_update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_ignores_non_private_chat():
    """handle_message must ignore group/supergroup/channel messages entirely."""
    from telegram_chat_bot import handle_message

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


import asyncio
import time

from unittest.mock import AsyncMock

from telegram_chat_bot import handle_message


@pytest.mark.asyncio
async def test_handle_message_rate_limiting():
    """A second request within the interval gets the wait reply and does NOT call
    the model or add history."""
    from telegram_chat_bot import Settings, State

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
    from telegram_chat_bot import Settings, State

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

    with patch("telegram_chat_bot.complete", side_effect=RuntimeError("model request failed")):
        await handle_message(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once_with(
        "I couldn\'t reach the local model. Please try again shortly."
    )
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
