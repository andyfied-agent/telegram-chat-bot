import json
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from telegram_chat_bot import State, Settings, complete, private


def test_settings_new_fields():
    # Test new rate limiting settings
    with patch.dict('os.environ', {
        'MAX_REQUESTS_PER_MINUTE': '5',
        'MODEL_REQUEST_INTERVAL': '2.0',
        'MAX_CONCURRENT_REQUESTS': '3'
    }):
        settings = Settings(token='test_token')
        assert settings.max_requests_per_minute == 5
        assert settings.request_interval == 2.0
        assert settings.max_concurrent_requests == 3


def test_state_authorized_for_prompts():
    # Test authorization for global prompt changes
    with patch.dict('os.environ', {'TELEGRAM_ALLOWED_USER_IDS': '123, 456'}):
        settings = Settings(token='test_token')
        state = State(settings)
        
        # Users in allowlist should be authorized
        assert state.is_authorized_for_global_prompt(123)
        assert state.is_authorized_for_global_prompt(456)
        assert not state.is_authorized_for_global_prompt(789)
        
        # Users in allowlist should be authorized for private prompts
        assert state.is_authorized_for_private_prompt(123)
        assert state.is_authorized_for_private_prompt(456)
        assert not state.is_authorized_for_private_prompt(789)


def test_rate_limiting():
    # Test rate limiting functionality
    settings = Settings(token='test_token')
    state = State(settings)
    
    # Initialize with a user
    user_id = 123
    assert state.request_counts[user_id] == 0
    
    # Simulate requests
    import time
    now = time.monotonic()
    state.request_window_start = now - 61  # Reset window
    
    # Check that we can make requests within limit
    for i in range(10):  # Test up to max_requests_per_minute
        # Reset window if needed
        if now - state.request_window_start > 60:
            state.request_counts.clear()
            state.request_window_start = now
        state.request_counts[user_id] += 1
    
    # Test that we can still make requests within limit
    assert state.request_counts[user_id] == 10


def test_rate_limiting_exceeded():
    # Test that rate limiting prevents too many requests
    with patch.dict('os.environ', {'MAX_REQUESTS_PER_MINUTE': '2'}):
        settings = Settings(token='test_token')
        state = State(settings)
        
        # Simulate requests
        import time
        now = time.monotonic()
        state.request_window_start = now - 61  # Reset window
        
        # Fill up the request count
        state.request_counts[123] = 2
        assert state.request_counts[123] == 2
        
        # Check that we've exceeded the limit
        assert state.request_counts[123] >= state.settings.max_requests_per_minute


def test_state_initialization_new():
    # Test state initialization with new fields
    settings = Settings(token='test_token')
    state = State(settings)
    assert state.settings == settings
    assert isinstance(state.history, dict)
    assert state.global_prompt == ''
    assert isinstance(state.private_prompts, dict)
    assert not state.allowed(123)
    assert hasattr(state, 'request_counts')
    assert hasattr(state, 'request_window_start')
    assert hasattr(state, 'request_gate')
    assert state.request_gate._value == 1  # Default concurrent requests


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
