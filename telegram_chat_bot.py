#!/usr/bin/env python3
"""Private-message Telegram bot backed by a local llama.cpp OpenAI API."""

import asyncio
import json
import logging
import os
import sys
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)




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
    payload = json.dumps({
        "model": settings.model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": settings.max_chars,
    }).encode()
    request = Request(f"{settings.base_url.rstrip('/')}/chat/completions", data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=settings.timeout) as response:
            data = json.loads(response.read())
        text = data["choices"][0]["message"].get("content", "").strip()
        if not text:
            raise ValueError("empty model response")
        return text[: settings.max_chars]
    except (HTTPError, URLError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("model request failed") from exc


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
    state.history.pop(update.effective_user.id, None)
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
    await update.message.reply_text("Private prompt updated.")


async def shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.application.bot_data["state"].allowed(update.effective_user.id):
        return
    await update.message.reply_text("Shutdown from Telegram is disabled; stop the local service instead.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.application.bot_data["state"].allowed(update.effective_user.id):
        return
    await update.message.reply_text("Commands: /start /approve <telegram_user_id> /reject <telegram_user_id> /users /status /reset /history /addglobalprompt <text> /addprivateprompt <text> /shutdown /help")


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
    except RuntimeError:
        state.history[user_id].pop()
        logger.exception("llama.cpp request failed for user %s", user_id)
        await update.message.reply_text("I couldn't reach the local model. Please try again shortly.")
        return
    state.history[user_id].append({"role": "assistant", "content": response})
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
    only_private = private()
    commands = {"start": start, "approve": approve, "reject": reject, "users": users,
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
