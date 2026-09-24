"""Tests for resilience features."""
from unittest.mock import MagicMock, patch
import json
import time
import pytest
from urllib.error import URLError

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
    log_provider_failure,
    resilience_status,
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
