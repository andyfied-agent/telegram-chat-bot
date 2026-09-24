#!/usr/bin/env python3
"""Private-message Telegram bot backed by a local llama.cpp OpenAI API."""

import asyncio
import json
import logging
import math
import os
import sys
import tempfile
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable
from enum import Enum
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Rotating file handler for Hermes logs (initialized lazily)
_log_dir: Path | None = None
_file_handler: RotatingFileHandler | None = None


def _init_logging() -> None:
    """Initialize rotating file handler lazily (only when first called)."""
    global _log_dir, _file_handler
    if _file_handler is not None:
        return  # Already initialized
    
    _log_dir = Path("/mnt/scratch/hermes/logs")
    try:
        _log_dir.mkdir(parents=True, exist_ok=True)
        log_file = _log_dir / "telegram-chat-bot.log"
        _file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10MB per file
            backupCount=5,  # Keep 5 backup files
            encoding="utf-8"
        )
        _file_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        logger.addHandler(_file_handler)
    except OSError as e:
        logger.warning("Failed to initialize rotating file handler: %s (logging to stdout only)", e)


def _ensure_logging_initialized() -> None:
    """Ensure logging is initialized before use."""
    if _file_handler is None:
        _init_logging()


@dataclass(frozen=True)
class ProviderFailure:
    """Record a provider failure without exposing credentials or URLs."""
    provider: str
    error_type: str
    timestamp: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return f"{self.provider} failed: {self.error_type} at {self.timestamp}"


# Global failure log (in-memory, no credentials/URLs stored)
_provider_failures: list[ProviderFailure] = []


def log_provider_failure(provider: str, error_type: str) -> None:
    """Log a provider failure without exposing credentials or request URLs."""
    _ensure_logging_initialized()
    failure = ProviderFailure(provider=provider, error_type=error_type)
    _provider_failures.append(failure)
    logger.warning("%s", failure)


def get_provider_failures() -> list[ProviderFailure]:
    """Return the list of recorded provider failures."""
    return _provider_failures[:]


class ProviderTimeoutError(RuntimeError):
    """Raised when the bounded provider request exceeds its timeout."""




class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    success_threshold: int = 2
    cooldown_seconds: float = 60.0
    half_open_max_requests: int = 2

    _state: CircuitState = CircuitState.CLOSED
    _failure_count: int = 0
    _success_count: int = 0
    _last_failure_time: float | None = None
    _half_open_requests: int = 0

    def record_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._close()

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._state == CircuitState.CLOSED and self._failure_count >= self.failure_threshold:
            self._open()
        elif self._state == CircuitState.HALF_OPEN:
            self._open()

    def _open(self) -> None:
        self._state = CircuitState.OPEN
        self._success_count = 0
        self._half_open_requests = 0

    def _close(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None

    def can_proceed(self) -> bool:
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            if self._last_failure_time and time.time() - self._last_failure_time >= self.cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                self._half_open_requests = 1
                return True
            return False
        if self._state == CircuitState.HALF_OPEN:
            if self._half_open_requests < self.half_open_max_requests:
                self._half_open_requests += 1
                return True
            return False
        return False

    def get_state(self) -> str:
        return self._state.value

    def get_stats(self) -> dict:
        return {
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure_time": self._last_failure_time,
            "half_open_requests": self._half_open_requests,
        }


@dataclass
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: bool = True
    retryable_errors: tuple[type[Exception], ...] = (ProviderTimeoutError, RuntimeError)


_provider_circuit_breakers: dict[str, "CircuitBreaker"] = {}


def get_circuit_breaker(provider: str) -> "CircuitBreaker":
    if provider not in _provider_circuit_breakers:
        _provider_circuit_breakers[provider] = CircuitBreaker()
    return _provider_circuit_breakers[provider]


def get_all_circuit_breaker_stats() -> dict[str, dict]:
    return {provider: cb.get_stats() for provider, cb in _provider_circuit_breakers.items()}


def _validate_numeric_setting(value: str, setting_name: str, *, as_float: bool = False) -> float:
    """Validate that *value* is a numeric string representing a positive number.

    Returns the parsed float.  Raises :exc:`ValueError` with the setting name
    in the message when the value is non-numeric, not positive, or special (nan/inf).
    """
    try:
        if as_float:
            num = float(value)
        else:
            num = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"{setting_name} must be a numeric value, got {value!r}")
    if num <= 0:
        raise ValueError(f"{setting_name} must be positive, got {num}")
    if math.isnan(num) or math.isinf(num):
        raise ValueError(f"{setting_name} must be a finite positive value, got {value!r}")
    return num


def _validate_limit_setting(value: str, setting_name: str) -> int:
    """Validate a daily limit, where zero means unlimited."""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{setting_name} must be a non-negative integer, got {value!r}")
    if limit < 0:
        raise ValueError(f"{setting_name} must be non-negative, got {limit}")
    return limit


def _parse_bool_setting(value: str, setting_name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{setting_name} must be a boolean, got {value!r}")


def _parse_provider_limits(value: str) -> dict[str, int]:
    """Parse JSON provider limits without accepting malformed configuration."""
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("PROVIDER_DAILY_LIMITS must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError("PROVIDER_DAILY_LIMITS must be a JSON object")
    limits: dict[str, int] = {}
    for provider, limit in parsed.items():
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("PROVIDER_DAILY_LIMITS provider names must be non-empty strings")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("PROVIDER_DAILY_LIMITS values must be non-negative integers")
        limits[provider] = limit
    return limits


_state_lock = asyncio.Lock()


def _write_state_atomic(state_file: str, data: dict) -> None:
    """Write state atomically with restrictive permissions (0o600)."""
    dir_path = Path(state_file).parent
    dir_path.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        dir=str(dir_path), prefix=".state-", suffix=".tmp"
    )
    temp_file = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, state_file)
    except Exception:
        try:
            temp_file.unlink()
        except OSError:
            pass
        raise


def _read_state(state_file: str) -> dict:
    """Read state from file, returning empty dict if not found."""
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _truncate_history_by_limit(history: list[dict], max_history: int) -> list[dict]:
    """Truncate history to max_history entries, keeping newest entries."""
    if len(history) <= max_history:
        return history
    # Retain the most recent messages.
    return history[-max_history:]


@dataclass(frozen=True)
class Settings:
    token: str
    base_url: str = field(default_factory=lambda: os.getenv("LLAMA_CPP_BASE_URL", "http://127.0.0.1:11438/v1"))
    model: str = field(default_factory=lambda: os.getenv("LLAMA_CPP_MODEL", "ministral-3-3b-64k-q4_k_m.gguf"))
    timeout: float = field(default_factory=lambda: _validate_numeric_setting(os.getenv("LLAMA_CPP_TIMEOUT", "120"), "LLAMA_CPP_TIMEOUT"))
    max_messages: int = field(default_factory=lambda: _validate_numeric_setting(os.getenv("MAX_CONTEXT_MESSAGES", "20"), "MAX_CONTEXT_MESSAGES"))
    max_chars: int = field(default_factory=lambda: _validate_numeric_setting(os.getenv("MAX_MESSAGE_CHARS", "8000"), "MAX_MESSAGE_CHARS"))
    allowed_user_ids: frozenset[int] = field(default_factory=lambda: frozenset(
        int(value.strip()) for value in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if value.strip()
    ))
    admin_user_ids: frozenset[int] = field(default_factory=lambda: frozenset(
        int(value.strip()) for value in os.getenv("TELEGRAM_ADMIN_USER_IDS", "").split(",") if value.strip()
    ))
    request_interval: float = field(default_factory=lambda: _validate_numeric_setting(os.getenv("MODEL_REQUEST_INTERVAL", "1.0"), "MODEL_REQUEST_INTERVAL", as_float=True))
    registration_file: str = field(default_factory=lambda: os.path.expanduser(
        os.getenv("TELEGRAM_REGISTRATION_FILE", "~/.local/state/telegram-chat-bot/registrations.json")
    ))
    state_file: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_BOT_STATE_FILE", os.path.expanduser("~/.telegram-chat-bot/state.json")))
    max_history: int = field(default_factory=lambda: _validate_numeric_setting(os.getenv("TELEGRAM_CHAT_BOT_MAX_HISTORY", "100"), "TELEGRAM_CHAT_BOT_MAX_HISTORY"))
    provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "llama.cpp"))
    default_daily_request_limit: int = field(default_factory=lambda: _validate_limit_setting(os.getenv("DEFAULT_DAILY_REQUEST_LIMIT", "0"), "DEFAULT_DAILY_REQUEST_LIMIT"))
    provider_daily_limits: dict[str, int] = field(default_factory=lambda: _parse_provider_limits(os.getenv("PROVIDER_DAILY_LIMITS", "{}")))
    openrouter_enabled: bool = field(default_factory=lambda: _parse_bool_setting(os.getenv("OPENROUTER_ENABLED", "false"), "OPENROUTER_ENABLED"))
    openrouter_daily_request_limit: int = field(default_factory=lambda: _validate_limit_setting(os.getenv("OPENROUTER_DAILY_REQUEST_LIMIT", "50"), "OPENROUTER_DAILY_REQUEST_LIMIT"))
    max_concurrent_requests: int = field(default_factory=lambda: _validate_numeric_setting(os.getenv("MAX_CONCURRENT_REQUESTS", "2"), "MAX_CONCURRENT_REQUESTS", as_float=False))

    # Resilience settings
    circuit_breaker_failure_threshold: int = field(default_factory=lambda: _validate_numeric_setting(os.getenv("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5"), "CIRCUIT_BREAKER_FAILURE_THRESHOLD"))
    circuit_breaker_success_threshold: int = field(default_factory=lambda: _validate_numeric_setting(os.getenv("CIRCUIT_BREAKER_SUCCESS_THRESHOLD", "2"), "CIRCUIT_BREAKER_SUCCESS_THRESHOLD"))
    circuit_breaker_cooldown_seconds: float = field(default_factory=lambda: _validate_numeric_setting(os.getenv("CIRCUIT_BREAKER_COOLDOWN_SECONDS", "60.0"), "CIRCUIT_BREAKER_COOLDOWN_SECONDS", as_float=True))
    max_retries: int = field(default_factory=lambda: _validate_numeric_setting(os.getenv("MAX_RETRIES", "3"), "MAX_RETRIES"))
    retry_base_delay: float = field(default_factory=lambda: _validate_numeric_setting(os.getenv("RETRY_BASE_DELAY", "1.0"), "RETRY_BASE_DELAY", as_float=True))
    retry_max_delay: float = field(default_factory=lambda: _validate_numeric_setting(os.getenv("RETRY_MAX_DELAY", "30.0"), "RETRY_MAX_DELAY", as_float=True))



class RegistrationStore:
    """Persist pending and approved Telegram user registrations atomically."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._users = json.loads(self.path.read_text())
            if not isinstance(self._users, dict):
                raise ValueError("registration store must contain an object")
        else:
            self._users: dict[str, str] = {}

    def status(self, user_id: int) -> str | None:
        return self._users.get(str(user_id))

    def set_status(self, user_id: int, status: str) -> None:
        if status not in {"pending", "approved", "rejected"}:
            raise ValueError(f"invalid registration status: {status}")
        self._users[str(user_id)] = status
        self._save()

    def users(self, status: str | None = None) -> list[int]:
        return sorted(int(user_id) for user_id, value in self._users.items() if status is None or value == status)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with open(fd, "w") as handle:
                json.dump(self._users, handle, sort_keys=True)
                handle.write("\n")
            Path(temporary).chmod(0o600)
            Path(temporary).replace(self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True)
class CompletionResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def complete_with_usage(settings: Settings, messages: list[dict[str, str]], provider: str | None = None) -> CompletionResult:
    """Make a model request with an independent timeout.

    Uses a ThreadPoolExecutor to run the HTTP request in a separate thread
    with a configurable timeout, ensuring a stalled provider cannot block
    the Telegram request.
    """
    provider = provider or settings.provider
    circuit_breaker = get_circuit_breaker(provider)
    retry_config = RetryConfig(
        max_retries=settings.max_retries,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
        jitter=True,
        retryable_errors=(ProviderTimeoutError, RuntimeError),
    )
    
    payload = json.dumps({
        "model": settings.model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": settings.max_chars,
    }).encode()

    def do_request():
        request = Request(f"{settings.base_url.rstrip('/')}/chat/completions", data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=settings.timeout) as response:
            data = json.loads(response.read())
        text = data["choices"][0]["message"].get("content", "").strip()
        if not text:
            raise ValueError("empty model response")
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            raise ValueError("invalid token usage in model response")
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        if (isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int)
                or prompt_tokens < 0 or isinstance(completion_tokens, bool)
                or not isinstance(completion_tokens, int) or completion_tokens < 0):
            raise ValueError("invalid token usage in model response")
        return CompletionResult(text=text[: settings.max_chars], prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens)

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(do_request)
    try:
        return future.result(timeout=settings.timeout)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise ProviderTimeoutError("model request timed out") from exc
    except (HTTPError, URLError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("model request failed") from exc
    finally:
        # Do not wait on a provider that has already exceeded the caller's
        # deadline. The request itself has the same socket timeout and the
        # future is cancelled when it has not started yet.
        executor.shutdown(wait=False, cancel_futures=True)


def complete(settings: Settings, messages: list[dict[str, str]]) -> str:
    """Return only the response text for callers using the legacy API."""
    return complete_with_usage(settings, messages).text


def health_check(base_url: str, timeout: float) -> tuple[str, bool]:
    """Return (model_name, is_online) from a bounded llama.cpp /health request.

    Safely derives the health URL from *base_url*, strips any trailing
    ``/v1`` path segment so the final URL is ``{base}/health``, and reads
    the response on a short timeout so it never blocks the async event
    loop.  Model name is read from the response JSON; secrets (tokens,
    private keys, etc.) are never exposed.
    """
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    url += "/health"
    request = Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read())
        model_name = data.get("model", "unknown")
        return model_name, True
    except Exception:
        return "unreachable", False


@dataclass
class UsageRecord:
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class UsageState:
    """Durable per-user, per-provider daily request and token accounting."""

    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self._clock = clock
        self.records: dict[str, dict[str, UsageRecord]] = defaultdict(dict)
        self.provider_requests: dict[str, dict[str, int]] = defaultdict(dict)

    def date_key(self) -> str:
        return datetime.fromtimestamp(self._clock()).date().isoformat()

    @staticmethod
    def _record_key(user_id: int, provider: str) -> str:
        return f"{user_id}:{provider}"

    def _get_record(self, user_id: int, provider: str, day: str | None = None) -> UsageRecord:
        day = day or self.date_key()
        key = self._record_key(user_id, provider)
        return self.records.setdefault(day, {}).setdefault(key, UsageRecord())

    def limit_for(self, provider: str) -> int:
        if provider == "openrouter" and self.settings.openrouter_enabled:
            return self.settings.default_daily_request_limit
        if provider in self.settings.provider_daily_limits:
            return self.settings.provider_daily_limits[provider]
        return self.settings.default_daily_request_limit

    def account_limit_for(self, provider: str) -> int:
        if provider != "openrouter" or not self.settings.openrouter_enabled:
            return 0
        return self.settings.provider_daily_limits.get(provider, self.settings.openrouter_daily_request_limit)

    def reserve_request(self, user_id: int, provider: str) -> tuple[bool, int, int]:
        """Reserve one outbound request and return (allowed, limit, remaining)."""
        record = self._get_record(user_id, provider)
        user_limit = self.limit_for(provider)
        account_limit = self.account_limit_for(provider)
        account_count = self.provider_requests.setdefault(self.date_key(), {}).get(provider, 0)
        if user_limit and record.request_count >= user_limit:
            return False, user_limit, 0
        if account_limit and account_count >= account_limit:
            return False, account_limit, 0
        record.request_count += 1
        if account_limit:
            self.provider_requests[self.date_key()][provider] = account_count + 1
        user_remaining = max(0, user_limit - record.request_count) if user_limit else None
        account_remaining = max(0, account_limit - account_count - 1) if account_limit else None
        remaining = min(value for value in (user_remaining, account_remaining) if value is not None) if (user_remaining is not None or account_remaining is not None) else 0
        return True, account_limit or user_limit, remaining

    def record_tokens(self, user_id: int, provider: str, prompt_tokens: int, completion_tokens: int) -> None:
        if (isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int) or prompt_tokens < 0
                or isinstance(completion_tokens, bool) or not isinstance(completion_tokens, int)
                or completion_tokens < 0):
            raise ValueError("token counts must be non-negative integers")
        record = self._get_record(user_id, provider)
        record.prompt_tokens += prompt_tokens
        record.completion_tokens += completion_tokens

    def stats(self, user_id: int, provider: str) -> dict[str, int | str | None]:
        record = self._get_record(user_id, provider)
        user_limit = self.limit_for(provider)
        account_limit = self.account_limit_for(provider)
        account_requests = self.provider_requests.get(self.date_key(), {}).get(provider, 0)
        limits = [value for value in (user_limit, account_limit) if value]
        remaining_values = []
        if user_limit:
            remaining_values.append(max(0, user_limit - record.request_count))
        if account_limit:
            remaining_values.append(max(0, account_limit - account_requests))
        remaining = min(remaining_values) if remaining_values else None
        effective_limit = min(limits) if limits else 0
        return {
            "date": self.date_key(), "user_id": user_id, "provider": provider,
            "request_count": record.request_count, "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens, "total_tokens": record.total_tokens,
            "limit": effective_limit, "remaining": remaining,
            "account_limit": account_limit, "account_requests": account_requests,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "records": {
                day: {
                key: {"request_count": record.request_count,
                      "prompt_tokens": record.prompt_tokens,
                      "completion_tokens": record.completion_tokens}
                for key, record in records.items()
                }
            for day, records in self.records.items()
            },
            "provider_requests": {
                day: dict(providers) for day, providers in self.provider_requests.items()
            },
        }

    def load_dict(self, data: object) -> None:
        if not isinstance(data, dict):
            return
        raw_records = data.get("records", data) if isinstance(data, dict) else {}
        raw_provider_requests = data.get("provider_requests", {}) if isinstance(data, dict) else {}
        for day, providers in raw_provider_requests.items():
            if not isinstance(day, str) or not isinstance(providers, dict):
                continue
            for provider, count in providers.items():
                if isinstance(provider, str) and isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    self.provider_requests[day][provider] = count
        for day, day_data in raw_records.items():
            if not isinstance(day, str) or not isinstance(day_data, dict):
                continue
            for key, raw_record in day_data.items():
                if not isinstance(key, str) or not isinstance(raw_record, dict):
                    continue
                counts = {name: raw_record.get(name, 0)
                          for name in ("request_count", "prompt_tokens", "completion_tokens")}
                if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                       for value in counts.values()):
                    continue
                self.records[day][key] = UsageRecord(**counts)


class State:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.history: dict[int, deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=settings.max_messages)
        )
        self.global_prompt = ""
        self.private_prompts: dict[int, str] = {}
        self.last_request: dict[int, float] = {}
        self.request_semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
        self.registrations = RegistrationStore(settings.registration_file)
        self.usage = UsageState(settings)
        self._persisted = False
        
        # Metrics tracking
        self.total_requests: int = 0
        self.successful_requests: int = 0
        self.failed_requests: int = 0
        self.total_response_time: float = 0.0
        self.user_request_count: dict[int, int] = defaultdict(int)
        
        # Rate-limit tracking: timestamps of requests in the last minute per user
        self.user_requests_minute: dict[int, deque[float]] = defaultdict(deque)

    def _load(self) -> None:
        """Load state from file on disk."""
        if self._persisted:
            return
        data = _read_state(self.settings.state_file)
        self.global_prompt = data.get("global_prompt", "")
        self.private_prompts = {int(k): v for k, v in data.get("private_prompts", {}).items()}
        user_history = data.get("user_history", {})
        for user_id_str, msgs in user_history.items():
            user_id = int(user_id_str)
            # Truncate to max_history limit if exceeded
            truncated = _truncate_history_by_limit(msgs, self.settings.max_history)
            self.history[user_id] = deque(truncated, maxlen=self.settings.max_messages)
        self.last_request = {int(k): v for k, v in data.get("last_request", {}).items()}
        self.usage.load_dict(data.get("usage", {}))
        self._persisted = True

    def _dump(self) -> None:
        """Dump state to file atomically with restrictive permissions."""
        data = {
            "global_prompt": self.global_prompt,
            "private_prompts": self.private_prompts,
            "user_history": {str(uid): list(msgs) for uid, msgs in self.history.items()},
            "last_request": self.last_request,
            "usage": self.usage.to_dict(),
        }
        _write_state_atomic(self.settings.state_file, data)

    async def persist(self) -> None:
        """Persist state to disk atomically."""
        async with _state_lock:
            self._dump()

    async def reset(self, user_id: int) -> None:
        """Reset user state: clear history and last_request."""
        self.history.pop(user_id, None)
        self.last_request.pop(user_id, None)
        await self.persist()

    def allowed(self, user_id: int) -> bool:
        return user_id in self.settings.allowed_user_ids or self.registrations.status(user_id) == "approved"

    def admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_user_ids

    def messages(self, user_id: int) -> list[dict[str, str]]:
        prompt = self.global_prompt
        if self.private_prompts.get(user_id):
            prompt = f"{prompt}\n{self.private_prompts[user_id]}".strip()
        result = ([{"role": "system", "content": prompt}] if prompt else [])
        result.extend(self.history[user_id])
        return result


def private() -> filters.BaseFilter:
    return filters.ChatType.PRIVATE


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    user_id = update.effective_user.id
    args = context.args if isinstance(context.args, (list, tuple)) else []
    if args:
        await update.message.reply_text(
            "This bot does not support /start parameters. Send /start without additional text to request access."
        )
        return
    if state.allowed(user_id):
        await update.message.reply_text("Hello! I only respond to private messages. Send me a message to chat.")
        return
    registration_status = state.registrations.status(user_id)
    if registration_status == "pending":
        await update.message.reply_text("Your access request is still pending administrator approval.")
        return
    if registration_status == "rejected":
        await update.message.reply_text("Your access request was rejected by an administrator.")
        return
    state.registrations.set_status(user_id, "pending")
    await update.message.reply_text("Your access request was recorded and is pending administrator approval.")


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    if not state.admin(update.effective_user.id):
        await update.message.reply_text("Only administrators can approve users.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /approve <telegram_user_id>")
        return
    user_id = int(context.args[0])
    state.registrations.set_status(user_id, "approved")
    await update.message.reply_text(f"User {user_id} approved.")


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    if not state.admin(update.effective_user.id):
        await update.message.reply_text("Only administrators can reject users.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /reject <telegram_user_id>")
        return
    user_id = int(context.args[0])
    state.registrations.set_status(user_id, "rejected")
    await update.message.reply_text(f"User {user_id} rejected.")


async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    if not state.admin(update.effective_user.id):
        await update.message.reply_text("Only administrators can revoke users.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /revoke <telegram_user_id>")
        return
    user_id = int(context.args[0])
    state.registrations.set_status(user_id, "rejected")
    await update.message.reply_text(f"User {user_id} revoked.")


async def users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    if not state.admin(update.effective_user.id):
        await update.message.reply_text("Only administrators can list users.")
        return
    pending = ", ".join(map(str, state.registrations.users("pending"))) or "none"
    approved = ", ".join(map(str, state.registrations.users("approved"))) or "none"
    await update.message.reply_text(f"Pending: {pending}\nApproved: {approved}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    if not state.allowed(update.effective_user.id):
        return
    model_name, is_online = await asyncio.to_thread(
        health_check, state.settings.base_url, 5
    )
    if is_online:
        await update.message.reply_text(
            f"Model {model_name} is online; retaining up to {state.settings.max_messages} messages."
        )
    else:
        await update.message.reply_text(
            "Model unreachable; cannot confirm health."
        )


async def metrics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    if not state.allowed(update.effective_user.id):
        return
    
    total = state.total_requests
    success = state.successful_requests
    failed = state.failed_requests
    avg_response_time = state.total_response_time / success if success > 0 else 0.0
    success_rate = (success / total * 100) if total > 0 else 0.0
    
    lines = [
        "📊 Bot Metrics:",
        f"Total requests: {total}",
        f"Successful: {success}",
        f"Failed: {failed}",
        f"Success rate: {success_rate:.1f}%",
        f"Average response time: {avg_response_time:.2f}s",
        "",
        "Concurrent limit:",
        f"Max concurrent: {state.settings.max_concurrent_requests}",
        f"Current semaphore value: {state.request_semaphore._value}",
    ]
    
    await update.message.reply_text("\n".join(lines))


async def ratelimit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    if not state.allowed(update.effective_user.id):
        return
    
    user_id = update.effective_user.id
    now = time.monotonic()
    
    # Clean old timestamps first
    cutoff = now - 60.0
    state.user_requests_minute[user_id] = deque(
        (t for t in state.user_requests_minute[user_id] if t > cutoff),
        maxlen=60
    )
    
    requests_in_minute = len(state.user_requests_minute[user_id])
    last_request_age = now - state.last_request.get(user_id, 0) if state.last_request.get(user_id) else float('inf')
    wait_time = max(0, state.settings.request_interval - last_request_age)
    
    lines = [
        "⏱️ Rate Limits:",
        f"User {user_id}:",
        f"  - Requests in last minute: {requests_in_minute}",
        f"  - Last request: {last_request_age:.1f}s ago",
        f"  - Cooldown: {state.settings.request_interval:.1f}s",
    ]
    
    if wait_time > 0:
        lines.append(f"  - Waiting: {wait_time:.1f}s")
    else:
        lines.append("  - Ready to send")
    
    lines.extend([
        "",
        "Global:",
        f"  - Max concurrent: {state.settings.max_concurrent_requests}",
        f"  - Available slots: {state.request_semaphore._value}",
    ])
    
    await update.message.reply_text("\n".join(lines))


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    if not state.allowed(update.effective_user.id):
        return
    await state.reset(update.effective_user.id)
    await update.message.reply_text("Your conversation context has been reset.")


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    if not state.allowed(update.effective_user.id):
        return
    await update.message.reply_text(f"I retain {len(state.history[update.effective_user.id])} recent messages in memory for this chat.")


async def addglobalprompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    user_id = update.effective_user.id
    if not state.admin(user_id):
        await update.message.reply_text("Only administrators can set the global prompt.")
        return
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text("Usage: /addglobalprompt <prompt>")
        return
    state.global_prompt = prompt[: state.settings.max_chars]
    await state.persist()
    await update.message.reply_text("Global prompt updated.")


async def addprivateprompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    if not state.allowed(update.effective_user.id):
        return
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text("Usage: /addprivateprompt <prompt>")
        return
    state.private_prompts[update.effective_user.id] = prompt[: state.settings.max_chars]
    await state.persist()
    await update.message.reply_text("Private prompt updated.")


async def shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.application.bot_data["state"].allowed(update.effective_user.id):
        return
    await update.message.reply_text("Shutdown from Telegram is disabled; stop the local service instead.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.application.bot_data["state"].allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "Commands: /start /approve <id> /reject <id> /revoke <id> /users /status /metrics /ratelimit /usage /reset /history /addglobalprompt <text> /addprivateprompt <text> /shutdown /help"
    )


async def usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    user_id = update.effective_user.id
    if not state.allowed(user_id):
        return
    stats = state.usage.stats(user_id, state.settings.provider)
    remaining = "unlimited" if stats["remaining"] is None else str(stats["remaining"])
    await update.message.reply_text(
        f"Usage for {stats['date']} ({stats['provider']}):\n"
        f"Requests: {stats['request_count']}/{stats['limit'] or 'unlimited'}\n"
        f"Remaining: {remaining}\n"
        f"Tokens: {stats['total_tokens']} ({stats['prompt_tokens']} prompt, "
        f"{stats['completion_tokens']} completion)"
    )



async def resilience_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show circuit breaker and resilience stats for all providers."""
    state: State = context.application.bot_data["state"]
    if not state.allowed(update.effective_user.id):
        return
    
    cb_stats = get_all_circuit_breaker_stats()
    failures = get_provider_failures()
    
    lines = ["Resilience Status:"]
    
    if cb_stats:
        for provider, stats in cb_stats.items():
            lines.append(f"  {provider}:")
            lines.append(f"    State: {stats['state']}")
            lines.append(f"    Failures: {stats['failure_count']}")
            if stats['last_failure_time']:
                since = time.time() - stats['last_failure_time']
                lines.append(f"    Last failure: {since:.0f}s ago")
    else:
        lines.append("  No circuit breakers recorded yet.")
    
    if failures:
        lines.append(f"\nRecent failures ({len(failures)}):")
        for f in failures[-5:]:
            lines.append(f"  - {f.provider}: {f.error_type}")
    
    await update.message.reply_text("\n".join(lines))


def _chunk(text: str, max_chunk: int = 4096) -> list[str]:
    """Split *text* into chunks of at most *max_chunk* characters."""
    if len(text) <= max_chunk:
        return [text]
    chunks: list[str] = []
    while text:
        chunks.append(text[:max_chunk])
        text = text[max_chunk:]
    return chunks


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_logging_initialized()
    if not update.message or not update.effective_chat or update.effective_chat.type != "private":
        return
    state: State = context.application.bot_data["state"]
    user_id = update.effective_user.id
    if not state.allowed(user_id):
        return
    text = (update.message.text or "").strip()[: state.settings.max_chars]
    if not text:
        return
    
    # Clean old request timestamps (> 1 minute ago)
    now = time.monotonic()
    cutoff = now - 60.0
    state.user_requests_minute[user_id] = deque(
        (t for t in state.user_requests_minute[user_id] if t > cutoff),
        maxlen=60
    )
    
    if now - state.last_request.get(user_id, 0) < state.settings.request_interval:
        await update.message.reply_text("Please wait a moment before sending another message.")
        return
    state.last_request[user_id] = now
    state.user_requests_minute[user_id].append(now)
    provider = state.settings.provider
    allowed, limit, _remaining = state.usage.reserve_request(user_id, provider)
    if not allowed:
        await update.message.reply_text(
            f"Daily request limit reached for {provider} ({limit} requests). Please try again tomorrow."
        )
        return
    state.total_requests += 1
    state.user_request_count[user_id] += 1
    state.history[user_id].append({"role": "user", "content": text})
    await state.persist()
    try:
        # Acquire semaphore with logging to track concurrent requests
        await state.request_semaphore.acquire()
        start_time = time.monotonic()
        try:
            result = await asyncio.to_thread(complete_with_usage, state.settings, state.messages(user_id))
            response_time = time.monotonic() - start_time
            state.successful_requests += 1
            state.total_response_time += response_time
        finally:
            state.request_semaphore.release()
    except ProviderTimeoutError:
        state.failed_requests += 1
        state.history[user_id].pop()
        log_provider_failure(provider, "timeout")
        logger.exception("llama.cpp request timed out for user %s", user_id)
        await state.persist()
        await update.message.reply_text("The model request timed out. Please try again shortly.")
        return
    except RuntimeError:
        state.failed_requests += 1
        state.history[user_id].pop()
        log_provider_failure(provider, "request_failed")
        logger.exception("llama.cpp request failed for user %s", user_id)
        await state.persist()
        await update.message.reply_text("I couldn't reach the local model. Please try again shortly.")
        return
    state.usage.record_tokens(user_id, provider, result.prompt_tokens, result.completion_tokens)
    state.history[user_id].append({"role": "assistant", "content": result.text})
    await state.persist()
    chunks = _chunk(result.text)
    for chunk in chunks:
        await update.message.reply_text(chunk)


def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN environment variable not set")
        sys.exit(1)
    settings = Settings(token=token)
    _ensure_logging_initialized()  # Initialize logging before starting
    app = Application.builder().token(token).build()
    app.bot_data["state"] = State(settings)
    # Load persisted state on startup
    app.bot_data["state"]._load()
    only_private = private()
    commands = {"start": start, "approve": approve, "reject": reject, "revoke": revoke, "users": users,
                "status": status, "metrics": metrics, "ratelimit": ratelimit, "reset": reset, "history": history,
                "addglobalprompt": addglobalprompt, "addprivateprompt": addprivateprompt,
                "shutdown": shutdown, "help": help_command, "usage": usage}
    for name, callback in commands.items():
        app.add_handler(CommandHandler(name, callback, filters=only_private))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Starting bot with llama.cpp model %s", settings.model)
    # Python 3.14 no longer creates the main-thread event loop implicitly.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()


if __name__ == "__main__":
    main()
