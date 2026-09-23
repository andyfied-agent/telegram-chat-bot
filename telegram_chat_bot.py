#!/usr/bin/env python3
"""Private-message Telegram bot backed by a local llama.cpp OpenAI API."""

import asyncio
import json
import logging
import os
import sys
import math
import tempfile
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


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
    failure = ProviderFailure(provider=provider, error_type=error_type)
    _provider_failures.append(failure)
    logger.warning("%s", failure)


def get_provider_failures() -> list[ProviderFailure]:
    """Return the list of recorded provider failures."""
    return _provider_failures[:]


class ProviderTimeoutError(RuntimeError):
    """Raised when the bounded provider request exceeds its timeout."""


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


def complete(settings: Settings, messages: list[dict[str, str]]) -> str:
    """Make a model request with an independent timeout.

    Uses a ThreadPoolExecutor to run the HTTP request in a separate thread
    with a configurable timeout, ensuring a stalled provider cannot block
    the Telegram request.
    """
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
        return text[: settings.max_chars]

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


class State:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.history: dict[int, deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=settings.max_messages)
        )
        self.global_prompt = ""
        self.private_prompts: dict[int, str] = {}
        self.last_request: dict[int, float] = {}
        self.request_gate = asyncio.Semaphore(1)
        self.registrations = RegistrationStore(settings.registration_file)
        self._persisted = False

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
        self._persisted = True

    def _dump(self) -> None:
        """Dump state to file atomically with restrictive permissions."""
        data = {
            "global_prompt": self.global_prompt,
            "private_prompts": self.private_prompts,
            "user_history": {str(uid): list(msgs) for uid, msgs in self.history.items()},
            "last_request": self.last_request,
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
    await update.message.reply_text("Commands: /start /approve <telegram_user_id> /reject <telegram_user_id> /revoke <telegram_user_id> /users /status /reset /history /addglobalprompt <text> /addprivateprompt <text> /shutdown /help")


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
    if not update.message or not update.effective_chat or update.effective_chat.type != "private":
        return
    state: State = context.application.bot_data["state"]
    user_id = update.effective_user.id
    if not state.allowed(user_id):
        return
    text = (update.message.text or "").strip()[: state.settings.max_chars]
    if not text:
        return
    now = time.monotonic()
    if now - state.last_request.get(user_id, 0) < state.settings.request_interval:
        await update.message.reply_text("Please wait a moment before sending another message.")
        return
    state.last_request[user_id] = now
    state.history[user_id].append({"role": "user", "content": text})
    try:
        async with state.request_gate:
            response = await asyncio.to_thread(complete, state.settings, state.messages(user_id))
    except ProviderTimeoutError:
        state.history[user_id].pop()
        log_provider_failure("llama.cpp", "timeout")
        logger.exception("llama.cpp request timed out for user %s", user_id)
        await update.message.reply_text("The model request timed out. Please try again shortly.")
        return
    except RuntimeError:
        state.history[user_id].pop()
        log_provider_failure("llama.cpp", "request_failed")
        logger.exception("llama.cpp request failed for user %s", user_id)
        await update.message.reply_text("I couldn't reach the local model. Please try again shortly.")
        return
    state.history[user_id].append({"role": "assistant", "content": response})
    await state.persist()
    chunks = _chunk(response)
    for chunk in chunks:
        await update.message.reply_text(chunk)


def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN environment variable not set")
        sys.exit(1)
    settings = Settings(token=token)
    app = Application.builder().token(token).build()
    app.bot_data["state"] = State(settings)
    # Load persisted state on startup
    app.bot_data["state"]._load()
    only_private = private()
    commands = {"start": start, "approve": approve, "reject": reject, "revoke": revoke, "users": users,
                "status": status, "reset": reset, "history": history,
                "addglobalprompt": addglobalprompt, "addprivateprompt": addprivateprompt,
                "shutdown": shutdown, "help": help_command}
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
