#!/usr/bin/env python3
"""Private-message Telegram bot backed by a local llama.cpp OpenAI API."""

import asyncio
import json
import logging
import math
import os
import random
import sys
import tempfile
import time
import threading
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

from dotenv import load_dotenv
from telegram import Update, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Load .env file if it exists (prior to any os.getenv calls)
# Prefer project-local .env over home directory .env
# Do NOT override already-exported environment variables
dotenv_path = Path(".env")
if dotenv_path.exists():
    load_dotenv(dotenv_path, override=False)

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
    
    # Configurable log directory via environment variable
    # Default to user state directory for portability (not compute01-specific /mnt/scratch)
    log_dir_str = os.getenv("TELEGRAM_BOT_LOG_DIR", str(Path.home() / ".local" / "state" / "telegram-chat-bot" / "logs"))
    _log_dir = Path(log_dir_str).expanduser()
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


class HTTPClientError(Exception):
    """Non-retryable HTTP client errors (4xx)."""
    pass


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
    lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    def __post_init__(self):
        # Ensure half_open_max_requests equals success_threshold for proper recovery
        if self.half_open_max_requests != self.success_threshold:
            object.__setattr__(self, 'half_open_max_requests', self.success_threshold)

    _state: CircuitState = CircuitState.CLOSED
    _failure_count: int = 0
    _success_count: int = 0
    _last_failure_time: float | None = None
    _half_open_requests: int = 0

    def record_success(self) -> None:
        with self.lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._close()
            elif self._state == CircuitState.CLOSED:
                # Reset failure count on success in CLOSED state to prevent
                # slow accumulation of failures (e.g., 5 isolated failures separated by
                # hundreds of successes could open the circuit)
                self._failure_count = 0
                self._last_failure_time = None
            # Note: In OPEN state, we do NOT reset _last_failure_time to preserve
            # the cooldown timer. A late success shouldn't prevent the breaker from
            # eventually recovering.

    def record_failure(self) -> None:
        with self.lock:
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
        with self.lock:
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
        with self.lock:
            return self._state.value

    def get_stats(self) -> dict:
        with self.lock:
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


# Global circuit breaker registry with lock for thread-safe creation
_provider_circuit_breakers: dict[str, CircuitBreaker] = {}
_provider_circuit_breakers_lock = threading.Lock()


def get_circuit_breaker(provider: str, settings: "Settings | None" = None) -> "CircuitBreaker":
    with _provider_circuit_breakers_lock:
        if provider not in _provider_circuit_breakers:
            if settings:
                _provider_circuit_breakers[provider] = CircuitBreaker(
                    failure_threshold=settings.circuit_breaker_failure_threshold,
                    success_threshold=settings.circuit_breaker_success_threshold,
                    cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
                    half_open_max_requests=2,  # equals success_threshold
                )
            else:
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


def _parse_user_rate_limits(value: str) -> dict[int, float]:
    """Parse JSON user rate limits without accepting malformed configuration."""
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("USER_RATE_LIMITS must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError("USER_RATE_LIMITS must be a JSON object")
    limits: dict[int, float] = {}
    for user_id_str, interval in parsed.items():
        try:
            user_id = int(user_id_str)
        except ValueError:
            raise ValueError(f"USER_RATE_LIMITS user IDs must be integers, got {user_id_str!r}")
        if isinstance(interval, bool):
            raise ValueError(f"USER_RATE_LIMITS values must be numbers, not booleans (true/false)")
        if not isinstance(interval, (int, float)):
            raise ValueError(f"USER_RATE_LIMITS values must be positive numbers, got {interval!r}")
        if interval <= 0:
            raise ValueError(f"USER_RATE_LIMITS values must be positive, got {interval!r}")
        # Reject NaN and Infinity
        if math.isnan(interval) or math.isinf(interval):
            raise ValueError(f"USER_RATE_LIMITS values must be finite numbers")
        limits[user_id] = float(interval)
    return limits


_state_lock = asyncio.Lock()


def remove_bot_mention(update_message, bot_username: str) -> str:
    """Remove the bot mention from message text using entity-aware parsing.
    
    Uses parse_entity to properly handle UTF-16 code unit offsets,
    avoiding issues with emoji/non-BMP characters before the mention.
    
    Args:
        update_message: Telegram message object with entities
        bot_username: The bot's username (without @)
    
    Returns:
        Message text with the bot mention removed.
    """
    if not update_message.entities:
        return update_message.text
    
    text = update_message.text
    # Find all entities that match the bot mention
    for entity in update_message.entities:
        mention_text = update_message.parse_entity(entity)
        if mention_text and mention_text.lower() == f"@{bot_username}".lower():
            # Remove the mention and strip
            return text.replace(mention_text, "").strip()
    
    return text


def has_mention_botname(update_message, bot_username: str) -> bool:
    """Check if message contains an actual @<bot_username> mention using Telegram entities.
    
    Args:
        update_message: The message object from Telegram update
        bot_username: The bot's username (without @)
    
    Returns:
        True if message contains an actual mention of this bot, False otherwise.
        Uses Telegram's message entities for accurate detection.
    """
    if not update_message.entities:
        return False
    
    # Telegram entities are case-sensitive, so we need to match exactly
    # but we'll also check case-insensitively for robustness
    for entity in update_message.entities:
        if entity.type == "mention":
            # Use parse_entity to properly handle UTF-16 code unit offsets
            mentioned_user = update_message.parse_entity(entity)
            if mentioned_user and mentioned_user.lstrip("@").lower() == bot_username.lower():
                return True
        elif entity.type == "bot_command":
            # Bot commands like @botname command are also mentions
            command_text = update_message.parse_entity(entity)
            if command_text and command_text.lower() == f"@{bot_username}".lower():
                return True
    
    return False


def _state_key(user_id: int, group_id: int | None = None) -> int | tuple[int, int]:
    """Return state key: user_id for private, (user_id, group_id) for groups."""
    return (user_id, group_id) if group_id is not None else user_id


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
    base_url: str = field(default_factory=lambda: os.getenv("LLAMA_CPP_BASE_URL", "http://127.0.0.1:8081/v1"))
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
    allowed_group_ids: frozenset[int] = field(default_factory=lambda: frozenset(
        int(value.strip()) for value in os.getenv("TELEGRAM_ALLOWED_GROUP_IDS", "").split(",") if value.strip()
    ))
    group_access_mode: str = field(default_factory=lambda: os.getenv("GROUP_ACCESS_MODE", "all"))
    request_interval: float = field(default_factory=lambda: _validate_numeric_setting(os.getenv("MODEL_REQUEST_INTERVAL", "1.0"), "MODEL_REQUEST_INTERVAL", as_float=True))
    registration_file: str = field(default_factory=lambda: os.path.expanduser(
        os.getenv("TELEGRAM_REGISTRATION_FILE", "~/.local/state/telegram-chat-bot/registrations.json")
    ))
    state_file: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_BOT_STATE_FILE", os.path.expanduser("~/.local/state/telegram-chat-bot/state.json")))
    # Read-only compatibility source for pre-XDG installations; new writes use state_file.
    legacy_state_file: str = field(default_factory=lambda: os.path.expanduser("~/.telegram-chat-bot/state.json"))
    max_history: int = field(default_factory=lambda: _validate_numeric_setting(os.getenv("TELEGRAM_CHAT_BOT_MAX_HISTORY", "100"), "TELEGRAM_CHAT_BOT_MAX_HISTORY"))
    provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "llama.cpp"))
    default_daily_request_limit: int = field(default_factory=lambda: _validate_limit_setting(os.getenv("DEFAULT_DAILY_REQUEST_LIMIT", "0"), "DEFAULT_DAILY_REQUEST_LIMIT"))
    provider_daily_limits: dict[str, int] = field(default_factory=lambda: _parse_provider_limits(os.getenv("PROVIDER_DAILY_LIMITS", "{}")))
    openrouter_enabled: bool = field(default_factory=lambda: _parse_bool_setting(os.getenv("OPENROUTER_ENABLED", "false"), "OPENROUTER_ENABLED"))
    openrouter_daily_request_limit: int = field(default_factory=lambda: _validate_limit_setting(os.getenv("OPENROUTER_DAILY_REQUEST_LIMIT", "50"), "OPENROUTER_DAILY_REQUEST_LIMIT"))
    max_concurrent_requests: int = field(default_factory=lambda: _validate_numeric_setting(os.getenv("MAX_CONCURRENT_REQUESTS", "2"), "MAX_CONCURRENT_REQUESTS", as_float=False))
    user_rate_limits: dict[int, float] = field(default_factory=lambda: _parse_user_rate_limits(os.getenv("USER_RATE_LIMITS", "{}")))
    
    # Resilience settings
    circuit_breaker_failure_threshold: int = field(default_factory=lambda: _validate_numeric_setting(os.getenv("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5"), "CIRCUIT_BREAKER_FAILURE_THRESHOLD"))
    circuit_breaker_success_threshold: int = field(default_factory=lambda: _validate_numeric_setting(os.getenv("CIRCUIT_BREAKER_SUCCESS_THRESHOLD", "2"), "CIRCUIT_BREAKER_SUCCESS_THRESHOLD"))
    circuit_breaker_cooldown_seconds: float = field(default_factory=lambda: _validate_numeric_setting(os.getenv("CIRCUIT_BREAKER_COOLDOWN_SECONDS", "60.0"), "CIRCUIT_BREAKER_COOLDOWN_SECONDS", as_float=True))
    max_retries: int = field(default_factory=lambda: _validate_numeric_setting(os.getenv("MAX_RETRIES", "3"), "MAX_RETRIES"))
    retry_base_delay: float = field(default_factory=lambda: _validate_numeric_setting(os.getenv("RETRY_BASE_DELAY", "1.0"), "RETRY_BASE_DELAY", as_float=True))
    retry_max_delay: float = field(default_factory=lambda: _validate_numeric_setting(os.getenv("RETRY_MAX_DELAY", "30.0"), "RETRY_MAX_DELAY", as_float=True))
    
    def __post_init__(self) -> None:
        """Validate group_access_mode after initialization."""
        if self.group_access_mode not in ("all", "approved_users", "admins"):
            raise ValueError(f"Invalid GROUP_ACCESS_MODE: {self.group_access_mode}. Must be one of: all, approved_users, admins")


class RegistrationStore:
    """Persist pending and approved Telegram user and group registrations atomically."""
    
    def __init__(self, path: str, group: bool = False) -> None:
        """
        Initialize registration store.
        
        Args:
            path: File path for storage
            group: If True, this is a group registration store; if False, user registration store
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.group = group
        if self.path.exists():
            self._users = json.loads(self.path.read_text())
            if not isinstance(self._users, dict):
                raise ValueError("registration store must contain an object")
        else:
            self._users: dict[str, str] = {}
    
    def status(self, user_id: int | str) -> str | None:
        """Return registration status for user or group ID."""
        return self._users.get(str(user_id))
    
    def set_status(self, user_id: int | str, status: str) -> None:
        """Set registration status for user or group ID."""
        if status not in {"pending", "approved", "rejected"}:
            raise ValueError(f"invalid registration status: {status}")
        self._users[str(user_id)] = status
        self._save()
    
    def users(self, status: str | None = None) -> list[int]:
        """Return list of registered users/groups, optionally filtered by status."""
        return sorted(int(user_id) for user_id, value in self._users.items() 
                     if status is None or value == status)
    
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
    circuit_breaker = get_circuit_breaker(provider, settings)
    # Define transient HTTP statuses that warrant retry (5xx only, not 4xx)
    transient_http_statuses = {500, 502, 503, 504}
    
    def should_retry_http_error(exc: HTTPError) -> bool:
        return exc.code in transient_http_statuses
    
    retry_config = RetryConfig(
        max_retries=settings.max_retries,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
        jitter=True,
        retryable_errors=(ProviderTimeoutError, RuntimeError, URLError),
        # HTTPError handling is special-cased below
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
    # Retry loop with circuit breaker
    last_exception: Exception | None = None
    attempt = 0

    while attempt <= retry_config.max_retries:
        # Check circuit breaker before each attempt
        if not circuit_breaker.can_proceed():
            raise ProviderTimeoutError(f"Circuit breaker OPEN for provider '{provider}'")

        try:
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(do_request)
            try:
                result = future.result(timeout=settings.timeout)
                # Success: record and return
                circuit_breaker.record_success()
                return result
            except FuturesTimeoutError as exc:
                # Timeout: record failure, cancel future
                circuit_breaker.record_failure()
                log_provider_failure(provider, "timeout")
                future.cancel()
                raise ProviderTimeoutError("model request timed out") from exc
            except HTTPError as exc:
                # Only retry transient HTTP errors (5xx), not client errors (4xx)
                if should_retry_http_error(exc):
                    circuit_breaker.record_failure()
                    log_provider_failure(provider, f"HTTP {exc.code}")
                    last_exception = exc
                else:
                    # Non-retryable: record failure and raise a special exception
                    circuit_breaker.record_failure()
                    log_provider_failure(provider, f"HTTP {exc.code}")
                    raise HTTPClientError(f"HTTP {exc.code}: {exc.reason}") from exc
            except retry_config.retryable_errors as exc:
                # Retryable error: record failure
                circuit_breaker.record_failure()
                log_provider_failure(provider, type(exc).__name__)
                last_exception = exc
            except HTTPClientError:
                # Already recorded, re-raise as-is
                raise
            except Exception as exc:
                # Non-retryable error: record failure, raise immediately
                circuit_breaker.record_failure()
                log_provider_failure(provider, type(exc).__name__)
                raise ProviderTimeoutError(f"provider error: {type(exc).__name__}") from exc
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        except ProviderTimeoutError:
            # If circuit breaker is now open, don't retry
            if not circuit_breaker.can_proceed():
                raise

        attempt += 1
        if attempt <= retry_config.max_retries:
            # Exponential backoff with jitter
            delay = min(retry_config.base_delay * (2 ** (attempt - 1)), retry_config.max_delay)
            if retry_config.jitter:
                delay *= (0.5 + random.random())
            time.sleep(delay)

    # Max retries exceeded
    # Raise with cause if we have one, otherwise without
    if last_exception is not None:
        raise ProviderTimeoutError(f"Provider '{provider}' failed after {retry_config.max_retries} retries") from last_exception
    raise ProviderTimeoutError(f"Provider '{provider}' failed after {retry_config.max_retries} retries")


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
        # Support both private (user_id only) and group (user_id, group_id tuple) keys
        self.history: dict[int | tuple[int, int], deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=settings.max_messages)
        )
        self.global_prompt = ""
        self.private_prompts: dict[int, str] = {}
        self.group_prompts: dict[int, str] = {}
        self.last_request: dict[int, float] = {}
        self.request_semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
        # Separate registration stores for users and groups
        self.user_registrations = RegistrationStore(settings.registration_file, group=False)
        self.group_registrations = RegistrationStore(str(Path(settings.registration_file).with_name("group_registrations.json")), group=True)
        self.usage = UsageState(settings)
        self._persisted = False
        
        # Rate-limit tracking: timestamps of requests in the last minute per user (private only)
        self.user_requests_minute: dict[int, deque[float]] = defaultdict(deque)
        
        # Metrics tracking
        self.total_requests: int = 0
        self.successful_requests: int = 0
        self.failed_requests: int = 0
        self.total_response_time: float = 0.0
        self.user_request_count: dict[int, int] = defaultdict(int)

    def _load(self) -> None:
        """Load state from file on disk."""
        if self._persisted:
            return
        state_path = Path(self.settings.state_file)
        data = _read_state(str(state_path))
        legacy_path = Path(self.settings.legacy_state_file)
        if not state_path.exists() and legacy_path.is_file():
            # Read the pre-XDG location once; future writes go to the XDG path.
            data = _read_state(str(legacy_path))
        self.global_prompt = data.get("global_prompt", "")
        self.private_prompts = {int(k): v for k, v in data.get("private_prompts", {}).items()}
        self.group_prompts = {int(k): v for k, v in data.get("group_prompts", {}).items()}
        user_history = data.get("user_history", {})
        for user_key_str, msgs in user_history.items():
            # Deserialize keys: if contains ":", it's "user_id:group_id" tuple; otherwise int
            if ":" in user_key_str:
                parts = user_key_str.split(":")
                if len(parts) == 2:
                    user_id = int(parts[0])
                    group_id = int(parts[1])
                    key = (user_id, group_id)
                else:
                    # Fallback: treat as user_id
                    key = int(user_key_str)
            else:
                key = int(user_key_str)
            # Truncate to max_history limit if exceeded
            truncated = _truncate_history_by_limit(msgs, self.settings.max_history)
            self.history[key] = deque(truncated, maxlen=self.settings.max_messages)
        self.last_request = {int(k): v for k, v in data.get("last_request", {}).items()}
        self.usage.load_dict(data.get("usage", {}))
        self._persisted = True

    def _dump(self) -> None:
        """Dump state to file atomically with restrictive permissions."""
        # Serialize history keys: int stays as int string, tuple becomes "user_id:group_id"
        user_history: dict[str, list[dict[str, str]]] = {}
        for key, msgs in self.history.items():
            if isinstance(key, tuple):
                user_key = f"{key[0]}:{key[1]}"
            else:
                user_key = str(key)
            user_history[user_key] = list(msgs)
        
        data = {
            "global_prompt": self.global_prompt,
            "private_prompts": self.private_prompts,
            "group_prompts": self.group_prompts,
            "user_history": user_history,
            "last_request": self.last_request,
            "usage": self.usage.to_dict(),
        }
        _write_state_atomic(self.settings.state_file, data)

    async def persist(self) -> None:
        """Persist state to disk atomically."""
        async with _state_lock:
            self._dump()

    async def reset(self, user_id: int, group_id: int | None = None) -> None:
        """Reset user state: clear history and last_request for user (and group if specified)."""
        key = _state_key(user_id, group_id)
        self.history.pop(key, None)
        self.last_request.pop(user_id, None)
        await self.persist()

    def allowed(self, user_id: int) -> bool:
        return user_id in self.settings.allowed_user_ids or self.user_registrations.status(user_id) == "approved"
    
    def user_status(self, user_id: int) -> str:
        """Return registration status for a user: 'unregistered', 'pending', 'rejected', or 'approved'."""
        if user_id in self.settings.allowed_user_ids:
            return "approved"
        return self.user_registrations.status(user_id) or "unregistered"
    
    def allowed_group(self, group_id: int, user_id: int | None = None) -> bool:
        """Check if group is allowed via TELEGRAM_ALLOWED_GROUP_IDS OR approved registration.
        
        Args:
            group_id: The group ID to check
            user_id: Optional user ID for access mode checks (required for approved_users/admins modes)
        
        Returns:
            True if the group is allowed for the given user (or any user if user_id not specified and mode is "all")
        """
        mode = self.settings.group_access_mode
        
        # Check static allowlist first
        if group_id in self.settings.allowed_group_ids:
            # If access mode is "all", any user can use it
            if mode == "all":
                return True
            # Otherwise check user authorization
            if user_id is not None:
                if mode == "approved_users":
                    return self.allowed(user_id)
                elif mode == "admins":
                    return self.admin(user_id)
            # If user_id not provided and mode is restricted, require it
            return user_id is not None
        
        # Check group registration
        if self.group_registrations.status(group_id) == "approved":
            # If access mode is "all", any user can use it
            if mode == "all":
                return True
            # Otherwise check user authorization
            if user_id is not None:
                if mode == "approved_users":
                    return self.allowed(user_id)
                elif mode == "admins":
                    return self.admin(user_id)
            # If user_id not provided and mode is restricted, require it
            return user_id is not None
        
        return False
    
    def admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_user_ids

    def messages(self, user_id: int, group_id: int | None = None) -> list[dict[str, str]]:
        """Return messages for user (and group if specified)."""
        key = _state_key(user_id, group_id)
        prompt = self.global_prompt
        # For group requests, use group prompt instead of private prompt
        if group_id is not None:
            # Group request: only use group prompt, never private prompt
            if self.group_prompts.get(group_id):
                prompt = f"{prompt}\n{self.group_prompts[group_id]}".strip()
        else:
            # Private request: use private prompt
            if self.private_prompts.get(user_id):
                prompt = f"{prompt}\n{self.private_prompts[user_id]}".strip()
        result = ([{"role": "system", "content": prompt}] if prompt else [])
        result.extend(self.history[key])
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
    registration_status = state.user_registrations.status(user_id)
    if registration_status == "pending":
        await update.message.reply_text("Your access request is still pending administrator approval.")
        return
    if registration_status == "rejected":
        await update.message.reply_text("Your access request was rejected by an administrator.")
        return
    state.user_registrations.set_status(user_id, "pending")
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
    state.user_registrations.set_status(user_id, "approved")
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
    state.user_registrations.set_status(user_id, "rejected")
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
    state.user_registrations.set_status(user_id, "rejected")
    await update.message.reply_text(f"User {user_id} revoked.")


async def users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    if not state.admin(update.effective_user.id):
        await update.message.reply_text("Only administrators can list users.")
        return
    
    pending = ", ".join(map(str, state.user_registrations.users("pending"))) or "none"
    approved = ", ".join(map(str, state.user_registrations.users("approved"))) or "none"
    await update.message.reply_text(f"Pending: {pending}\nApproved: {approved}")


# Group registration commands (Phase 2) - all private commands used by admins
async def startgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Request to add bot to a group. Admins use this command privately."""
    state: State = context.application.bot_data["state"]
    
    if not state.admin(update.effective_user.id):
        await update.message.reply_text("Only administrators can register groups.")
        return
    
    # Get group ID from command argument (negative group ID)
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /startgroup <telegram_group_id>")
        return
    
    group_id_str = context.args[0]
    try:
        group_id = int(group_id_str)
    except ValueError:
        await update.message.reply_text("Group ID must be a number (e.g., -100123456789)")
        return
    
    # Telegram group IDs are negative, so this is valid
    state.group_registrations.set_status(group_id, "pending")
    await update.message.reply_text(
        f"Group {group_id} registration request created. "
        f"Once approved, the bot will respond to @botname mentions in this group."
    )


async def approvegroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve a group registration. Used by admins in private messages."""
    state: State = context.application.bot_data["state"]
    
    if not state.admin(update.effective_user.id):
        await update.message.reply_text("Only administrators can approve groups.")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /approvegroup <telegram_group_id>")
        return
    
    group_id_str = context.args[0]
    try:
        group_id = int(group_id_str)
    except ValueError:
        await update.message.reply_text("Group ID must be a number (e.g., -100123456789)")
        return
    
    state.group_registrations.set_status(group_id, "approved")
    await update.message.reply_text(f"Group {group_id} approved. The bot will now respond in this group.")


async def rejectgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reject a group registration. Used by admins in private messages."""
    state: State = context.application.bot_data["state"]
    
    if not state.admin(update.effective_user.id):
        await update.message.reply_text("Only administrators can reject groups.")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /rejectgroup <telegram_group_id>")
        return
    
    group_id_str = context.args[0]
    try:
        group_id = int(group_id_str)
    except ValueError:
        await update.message.reply_text("Group ID must be a number (e.g., -100123456789)")
        return
    
    state.group_registrations.set_status(group_id, "rejected")
    await update.message.reply_text(f"Group {group_id} rejected.")


async def revokegroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revoke access for a group. Used by admins in private messages."""
    state: State = context.application.bot_data["state"]
    
    if not state.admin(update.effective_user.id):
        await update.message.reply_text("Only administrators can revoke groups.")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /revokegroup <telegram_group_id>")
        return
    
    group_id_str = context.args[0]
    try:
        group_id = int(group_id_str)
    except ValueError:
        await update.message.reply_text("Group ID must be a number (e.g., -100123456789)")
        return
    
    state.group_registrations.set_status(group_id, "rejected")
    await update.message.reply_text(f"Group {group_id} access revoked.")


async def groupusers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List registered groups. Used by admins in private messages."""
    state: State = context.application.bot_data["state"]
    
    if not state.admin(update.effective_user.id):
        await update.message.reply_text("Only administrators can list groups.")
        return
    
    pending = ", ".join(map(str, state.group_registrations.users("pending"))) or "none"
    approved = ", ".join(map(str, state.group_registrations.users("approved"))) or "none"
    rejected = ", ".join(map(str, state.group_registrations.users("rejected"))) or "none"
    await update.message.reply_text(
        f"Group Registrations:\n"
        f"Pending: {pending}\n"
        f"Approved: {approved}\n"
        f"Rejected: {rejected}"
    )


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
    
    # Use per-user rate limit or fall back to global
    user_rate_limit = state.settings.user_rate_limits.get(user_id, state.settings.request_interval)
    wait_time = max(0, user_rate_limit - last_request_age)
    
    lines = [
        "⏱️ Rate Limits:",
        f"User {user_id}:",
        f"  - Requests in last minute: {requests_in_minute}",
        f"  - Last request: {last_request_age:.1f}s ago",
        f"  - Cooldown: {user_rate_limit:.1f}s",
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


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check llama.cpp service health."""
    state: State = context.application.bot_data["state"]
    if not state.allowed(update.effective_user.id):
        return
    
    # Use a short timeout to avoid blocking the event loop
    health_timeout = min(5.0, state.settings.timeout / 10)
    model_name, is_online = await asyncio.to_thread(
        health_check, state.settings.base_url, health_timeout
    )
    
    # Show the configured model name, not what /health returns (which may not include it)
    model_name = state.settings.model
    
    lines = [
        "🏥 Health Check:",
        f"Model: {model_name}",
        f"Status: {'✅ Online' if is_online else '❌ Offline'}",
        f"Endpoint: {state.settings.base_url}",
    ]
    
    await update.message.reply_text("\n".join(lines))


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    
    # Determine group_id if in a group
    group_id = update.effective_chat.id if chat_type in ("group", "supergroup") else None
    
    # Check authorization based on chat type
    if chat_type == "private":
        if not state.allowed(user_id):
            return
    elif group_id:
        if not state.allowed_group(group_id, user_id):
            return
    else:
        return
    
    await state.reset(user_id, group_id)
    if group_id:
        await update.message.reply_text("Your conversation context for this group has been reset.")
    else:
        await update.message.reply_text("Your conversation context has been reset.")


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    
    # Determine group_id if in a group
    group_id = update.effective_chat.id if chat_type in ("group", "supergroup") else None
    
    # Check authorization based on chat type
    if chat_type == "private":
        if not state.allowed(user_id):
            return
    elif group_id:
        if not state.allowed_group(group_id, user_id):
            return
    else:
        return
    
    key = _state_key(user_id, group_id)
    history_count = len(state.history.get(key, []))
    if group_id:
        await update.message.reply_text(f"I retain {history_count} recent messages in memory for this group chat.")
    else:
        await update.message.reply_text(f"I retain {history_count} recent messages in memory for this chat.")


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


# Group prompt management commands
async def setgroupprompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a prompt for a specific group (admin only)."""
    state: State = context.application.bot_data["state"]
    user_id = update.effective_user.id
    if not state.admin(user_id):
        await update.message.reply_text("Only administrators can set group prompts.")
        return
    
    # Always expect: /setgroupprompt <group_id> <prompt>
    if not context.args:
        await update.message.reply_text("Usage: /setgroupprompt <group_id> <prompt>")
        return
    
    # First arg is always group_id
    try:
        group_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /setgroupprompt <group_id> <prompt>")
        return
    
    # Remaining args are the prompt
    prompt = " ".join(context.args[1:]).strip()
    if not prompt:
        await update.message.reply_text("Usage: /setgroupprompt <group_id> <prompt>")
        return
    
    state.group_prompts[group_id] = prompt[: state.settings.max_chars]
    await state.persist()
    await update.message.reply_text(f"Group prompt set for group {group_id}.")


async def cleargroupprompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear a group prompt (admin only)."""
    state: State = context.application.bot_data["state"]
    user_id = update.effective_user.id
    if not state.admin(user_id):
        await update.message.reply_text("Only administrators can clear group prompts.")
        return
    
    # Always expect: /cleargroupprompt <group_id>
    if not context.args:
        await update.message.reply_text("Usage: /cleargroupprompt <group_id>")
        return
    
    try:
        group_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /cleargroupprompt <group_id>")
        return
    
    if group_id in state.group_prompts:
        del state.group_prompts[group_id]
        await state.persist()
        await update.message.reply_text(f"Group prompt cleared for group {group_id}.")
    else:
        await update.message.reply_text(f"No group prompt found for group {group_id}.")


async def showgroupprompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a group prompt (admin only)."""
    state: State = context.application.bot_data["state"]
    user_id = update.effective_user.id
    if not state.admin(user_id):
        await update.message.reply_text("Only administrators can view group prompts.")
        return
    
    # Always expect: /showgroupprompt <group_id>
    if not context.args:
        await update.message.reply_text("Usage: /showgroupprompt <group_id>")
        return
    
    try:
        group_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /showgroupprompt <group_id>")
        return
    
    if group_id in state.group_prompts:
        await update.message.reply_text(f"Group prompt for {group_id}: {state.group_prompts[group_id]}")
    else:
        await update.message.reply_text(f"No group prompt found for group {group_id}.")


async def shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.application.bot_data["state"].allowed(update.effective_user.id):
        return
    await update.message.reply_text("Shutdown from Telegram is disabled; stop the local service instead.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.application.bot_data["state"].allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "Commands:\n"
        "/start - Request access or check status\n"
        "/approve <id> - Approve a user (admin only)\n"
        "/reject <id> - Reject a user (admin only)\n"
        "/revoke <id> - Revoke user access (admin only)\n"
        "/users - List all registrations\n"
        "/status - Show model and history info\n"
        "/metrics - Show request statistics\n"
        "/ratelimit - Show rate limit status\n"
        "/usage - Show daily usage\n"
        "/resilience - Show resilience/circuit breaker status\n"
        "/reset - Clear conversation history\n"
        "/history - Show message count\n"
        "/addglobalprompt <text> - Set global system prompt (admin only)\n"
        "/addprivateprompt <user_id> <text> - Set per-user private prompt (admin only)\n"
        "/setgroupprompt <group_id> <text> - Set group-specific prompt (admin only)\n"
        "/showgroupprompt <group_id> - Show group prompt (admin only)\n"
        "/cleargroupprompt <group_id> - Clear group prompt (admin only)\n"
        "/shutdown - Shutdown the bot (local only)\n"
        "/help - Show this message\n"
        "\n"
        "Group commands (work in groups):\n"
        "/reset - Clear group conversation history\n"
        "/history - Show message count in group\n"
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
    if not update.message or not update.effective_chat:
        return
    
    state: State = context.application.bot_data["state"]
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    group_id = update.effective_chat.id if chat_type in ("group", "supergroup") else None
    
    # Private message handling (existing behavior)
    if chat_type == "private":
        user_status = state.user_status(user_id)
        
        # Send status-based messages for non-approved users
        if user_status == "unregistered":
            await update.message.reply_text(
                "You don't have access yet. Send `/start` to request access."
            )
            return
        elif user_status == "pending":
            await update.message.reply_text(
                "Your access request is pending approval. Please wait for an administrator to approve it."
            )
            return
        elif user_status == "rejected":
            await update.message.reply_text(
                "Your access request was rejected. If you believe this is a mistake, please contact an administrator."
            )
            return
        # If approved, continue with normal message handling below
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
        
        # Check per-user rate limit or fall back to global setting
        user_rate_limit = state.settings.user_rate_limits.get(user_id, state.settings.request_interval)
        if now - state.last_request.get(user_id, 0) < user_rate_limit:
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
        # Store reference to the exact dict object appended, for robust removal on failure
        user_msg_dict = {"role": "user", "content": text}
        state.history[user_id].append(user_msg_dict)
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
        except (ProviderTimeoutError, RuntimeError, HTTPClientError):
            state.failed_requests += 1
            # Remove the exact dict object we appended (identity-based, survives identical messages)
            for index, message in enumerate(state.history[user_id]):
                if message is user_msg_dict:
                    del state.history[user_id][index]
                    break
            log_provider_failure(provider, "http_error" if isinstance(sys.exc_info()[1], HTTPClientError) else "unknown")
            logger.exception("llama.cpp request failed for user %s", user_id)
            await state.persist()
            # Provide more helpful error messages based on error type
            if "timeout" in str(sys.exc_info()[1]).lower() or "timed out" in str(sys.exc_info()[1]).lower():
                await update.message.reply_text("The model request timed out. The server may be busy. Please try again in a moment.")
            elif isinstance(sys.exc_info()[1], HTTPClientError):
                await update.message.reply_text("The model server returned an error. Please try again shortly.")
            else:
                await update.message.reply_text("The model request failed. Please try again shortly.")
            return
        state.usage.record_tokens(user_id, provider, result.prompt_tokens, result.completion_tokens)
        state.history[user_id].append({"role": "assistant", "content": result.text})
        await state.persist()
        chunks = _chunk(result.text)
        for chunk in chunks:
            await update.message.reply_text(chunk)
    
    # Group chat message handling (new)
    elif chat_type in ("group", "supergroup"):
        # Check if group is allowed (static allowlist OR approved registration)
        if not state.allowed_group(group_id, user_id):
            return  # Silent ignore for disallowed groups
        
        # Get bot username for @mention check
        bot_username = context.bot.username if context.bot else "telegram_bot"
        
        # Get full text from entity-aware parsing, then apply max_chars limit
        clean_text = remove_bot_mention(update.message, bot_username)
        # Enforce max_chars limit (same as private messages)
        clean_text = clean_text[: state.settings.max_chars]
        
        # Check @mention requirement
        if not has_mention_botname(update.message, bot_username):
            return  # Silent ignore without @mention
        
        if not clean_text:
            return  # Message was only the bot mention
        
        # Clean old request timestamps (> 1 minute ago)
        now = time.monotonic()
        cutoff = now - 60.0
        state.user_requests_minute[user_id] = deque(
            (t for t in state.user_requests_minute[user_id] if t > cutoff),
            maxlen=60
        )
        
        # Check per-user rate limit or fall back to global setting
        user_rate_limit = state.settings.user_rate_limits.get(user_id, state.settings.request_interval)
        if now - state.last_request.get(user_id, 0) < user_rate_limit:
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
        # Store reference to the exact dict object appended, for robust removal on failure
        user_msg_dict = {"role": "user", "content": clean_text}
        state.history[(user_id, group_id)].append(user_msg_dict)
        await state.persist()
        try:
            # Acquire semaphore with logging to track concurrent requests
            await state.request_semaphore.acquire()
            start_time = time.monotonic()
            try:
                result = await asyncio.to_thread(complete_with_usage, state.settings, state.messages(user_id, group_id))
                response_time = time.monotonic() - start_time
                state.successful_requests += 1
                state.total_response_time += response_time
            finally:
                state.request_semaphore.release()
        except (ProviderTimeoutError, RuntimeError, HTTPClientError):
            state.failed_requests += 1
            # Remove the exact dict object we appended (identity-based, survives identical messages)
            key = (user_id, group_id)
            for index, message in enumerate(state.history[key]):
                if message is user_msg_dict:
                    del state.history[key][index]
                    break
            log_provider_failure(provider, "http_error" if isinstance(sys.exc_info()[1], HTTPClientError) else "unknown")
            logger.exception("llama.cpp request failed for user %s in group %s", user_id, group_id)
            await state.persist()
            # Provide more helpful error messages based on error type
            if "timeout" in str(sys.exc_info()[1]).lower() or "timed out" in str(sys.exc_info()[1]).lower():
                await update.message.reply_text("The model request timed out. The server may be busy. Please try again in a moment.")
            elif isinstance(sys.exc_info()[1], HTTPClientError):
                await update.message.reply_text("The model server returned an error. Please try again shortly.")
            else:
                await update.message.reply_text("The model request failed. Please try again shortly.")
            return
        state.usage.record_tokens(user_id, provider, result.prompt_tokens, result.completion_tokens)
        state.history[(user_id, group_id)].append({"role": "assistant", "content": result.text})
        await state.persist()
        chunks = _chunk(result.text)
        for chunk in chunks:
            await update.message.reply_text(chunk)


async def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN environment variable not set")
        sys.exit(1)
    settings = Settings(token=token)
    _ensure_logging_initialized()  # Initialize logging before starting
    app = Application.builder().token(token).build()
    app.bot_data["state"] = State(settings)
    # Load persisted state on startup
    await app.initialize()  # Must initialize before set_my_commands
    app.bot_data["state"]._load()
    
    # Set up Telegram native command menu (async) - after app.initialize()
    commands = [
        BotCommand("start", "Request access or check your status"),
        BotCommand("approve", "Approve a user (admin only)"),
        BotCommand("reject", "Reject a user (admin only)"),
        BotCommand("revoke", "Revoke user access (admin only)"),
        BotCommand("users", "List registrations (admin only)"),
        BotCommand("status", "Show model and history info"),
        BotCommand("metrics", "Show request statistics"),
        BotCommand("ratelimit", "Show rate limit status"),
        BotCommand("health", "Check llama.cpp service health"),
        BotCommand("usage", "Show daily usage"),
        BotCommand("resilience", "Show resilience/circuit breaker status"),
        BotCommand("reset", "Clear conversation history"),
        BotCommand("history", "Show message count in memory"),
        BotCommand("addglobalprompt", "Set global system prompt (admin only)"),
        BotCommand("addprivateprompt", "Set per-user private prompt"),
        BotCommand("setgroupprompt", "Set group-specific prompt (admin only)"),
        BotCommand("showgroupprompt", "Show group prompt (admin only)"),
        BotCommand("cleargroupprompt", "Clear group prompt (admin only)"),
        BotCommand("shutdown", "Shutdown the bot (local only)"),
        BotCommand("help", "Show all available commands"),
    ]
    # Register commands for private chats
    await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    # Register group-specific commands - only expose commands that work in groups
    group_commands = [
        BotCommand("reset", "Clear group conversation history"),
        BotCommand("history", "Show message count in group"),
        BotCommand("setgroupprompt", "Set group-specific prompt (admin only)"),
        BotCommand("showgroupprompt", "Show group prompt (admin only)"),
        BotCommand("cleargroupprompt", "Clear group prompt (admin only)"),
        BotCommand("help", "Show all available commands"),
    ]
    await app.bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
    
    only_private = private()
    # Private-only commands (admin/user)
    private_commands = {"start": start, "approve": approve, "reject": reject, "revoke": revoke, "users": users,
                "status": status, "metrics": metrics, "ratelimit": ratelimit, "health": health,
                "addglobalprompt": addglobalprompt, "addprivateprompt": addprivateprompt,
                "shutdown": shutdown, "help": help_command, "usage": usage, "resilience": resilience_status}
    for name, callback in private_commands.items():
        app.add_handler(CommandHandler(name, callback, filters=only_private))
    # Commands that work in both private and group chats (reset, history)
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("history", history))
    # Group prompt commands - can be used in private with group_id arg
    app.add_handler(CommandHandler("setgroupprompt", setgroupprompt))
    app.add_handler(CommandHandler("cleargroupprompt", cleargroupprompt))
    app.add_handler(CommandHandler("showgroupprompt", showgroupprompt))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Group chat message handler (supports group and supergroup types)
    app.add_handler(MessageHandler((filters.ChatType.GROUP | filters.ChatType.SUPERGROUP) & filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Group registration commands (Phase 2) - all private commands used by admins
    group_commands = {"startgroup": startgroup, "approvegroup": approvegroup, 
                      "rejectgroup": rejectgroup, "revokegroup": revokegroup, "groupusers": groupusers}
    for name, callback in group_commands.items():
        app.add_handler(CommandHandler(name, callback, filters=only_private))
    
    logger.info("Starting bot with llama.cpp model %s", settings.model)
    # PTB lifecycle: initialize → updater polling → application start → updater stop → application stop → shutdown
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    try:
        while app.running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
