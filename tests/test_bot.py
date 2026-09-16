import asyncio
import json
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Message, Update, User
from telegram.ext import ContextTypes

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


def test_settings_custom():
    # Test custom settings
    with patch.dict('os.environ', {
        'LLAMA_CPP_BASE_URL': 'http://localhost:8080/v1',
        'LLAMA_CPP_MODEL': 'custom-model',
        'LLAMA_CPP_TIMEOUT': '300',
        'MAX_CONTEXT_MESSAGES': '10',
        'MAX_MESSAGE_CHARS': '4000'
    }):
        settings = Settings(token='test_token')
        assert settings.base_url == 'http://localhost:8080/v1'
        assert settings.model == 'custom-model'
        assert settings.timeout == 300.0
        assert settings.max_messages == 10
        assert settings.max_chars == 4000


def test_state_initialization():
    # Test state initialization
    settings = Settings(token='test_token')
    state = State(settings)
    assert state.settings == settings
    assert isinstance(state.history, dict)
    assert state.global_prompt == ''
    assert isinstance(state.private_prompts, dict)


def test_state_messages():
    # Test messages generation
    settings = Settings(token='test_token')
    state = State(settings)
    state.global_prompt = 'Global prompt'
    state.private_prompts[12345] = 'Private prompt'

    # Test with user ID that has no history
    messages = state.messages(12345)
    assert len(messages) == 2
    assert messages[0]['role'] == 'system'
    assert messages[0]['content'] == 'Global prompt\nPrivate prompt'
    assert messages[1]['role'] == 'user'
    assert messages[1]['content'] == 'Test message'

    # Test with user ID that has history
    state.history[12345].append({'role': 'user', 'content': 'Previous message'})
    messages = state.messages(12345)
    assert len(messages) == 3
    assert messages[2]['role'] == 'user'
    assert messages[2]['content'] == 'Previous message'


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
async def test_complete_success(mock_urlopen):
    # Test successful completion
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        'choices': [{'message': {'content': 'Test response'}}]
    }).encode()
    mock_urlopen.return_value.__enter__.return_value = mock_response

    settings = Settings(token='test_token')
    messages = [{'role': 'user', 'content': 'Test message'}]
    result = await asyncio.to_thread(complete, settings, messages)
    assert result == 'Test response'


@patch('telegram_chat_bot.urlopen')
async def test_complete_empty_response(mock_urlopen):
    # Test empty response
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        'choices': [{'message': {'content': ''}}]
    }).encode()
    mock_urlopen.return_value.__enter__.return_value = mock_response

    settings = Settings(token='test_token')
    messages = [{'role': 'user', 'content': 'Test message'}]
    with pytest.raises(RuntimeError, match='empty model response'):
        await asyncio.to_thread(complete, settings, messages)


@patch('telegram_chat_bot.urlopen')
async def test_complete_http_error(mock_urlopen):
    # Test HTTP error
    mock_urlopen.side_effect = Exception('HTTP Error')

    settings = Settings(token='test_token')
    messages = [{'role': 'user', 'content': 'Test message'}]
    with pytest.raises(RuntimeError, match='model request failed'):
        await asyncio.to_thread(complete, settings, messages)


@patch('telegram_chat_bot.urlopen')
async def test_complete_json_error(mock_urlopen):
    # Test JSON decode error
    mock_response = MagicMock()
    mock_response.read.return_value = b'invalid json'
    mock_urlopen.return_value.__enter__.return_value = mock_response

    settings = Settings(token='test_token')
    messages = [{'role': 'user', 'content': 'Test message'}]
    with pytest.raises(RuntimeError, match='model request failed'):
        await asyncio.to_thread(complete, settings, messages)
