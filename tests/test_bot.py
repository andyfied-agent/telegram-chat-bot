import json
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from telegram_chat_bot import State, Settings, complete, private


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
