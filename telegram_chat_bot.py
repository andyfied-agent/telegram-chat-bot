#!/usr/bin/env python3
"""Private-message Telegram bot backed by a local llama.cpp OpenAI API."""

import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    token: str
    base_url: str = field(default_factory=lambda: os.getenv("LLAMA_CPP_BASE_URL", "http://127.0.0.1:11438/v1"))
    model: str = field(default_factory=lambda: os.getenv("LLAMA_CPP_MODEL", "ministral-3-3b-64k-q4_k_m.gguf"))
    timeout: float = field(default_factory=lambda: float(os.getenv("LLAMA_CPP_TIMEOUT", "120")))
    max_messages: int = field(default_factory=lambda: int(os.getenv("MAX_CONTEXT_MESSAGES", "20")))
    max_chars: int = field(default_factory=lambda: int(os.getenv("MAX_MESSAGE_CHARS", "8000")))
    allowed_user_ids: frozenset[int] = field(default_factory=lambda: frozenset(
        int(value.strip()) for value in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if value.strip()
    ))
    request_interval: float = field(default_factory=lambda: float(os.getenv("MODEL_REQUEST_INTERVAL", "1.0")))


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

    def allowed(self, user_id: int) -> bool:
        return user_id in self.settings.allowed_user_ids

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
    if not context.application.bot_data["state"].allowed(update.effective_user.id):
        return
    await update.message.reply_text("Hello! I only respond to private messages. Send me a message to chat.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: State = context.application.bot_data["state"]
    if not state.allowed(update.effective_user.id):
        return
    await update.message.reply_text(f"Running {state.settings.model}; retaining up to {state.settings.max_messages} messages.")


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
    if not state.allowed(update.effective_user.id):
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
    await update.message.reply_text("Commands: /start /status /reset /history /addglobalprompt <text> /addprivateprompt <text> /help")


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
    await update.message.reply_text(response[:4096])


def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN environment variable not set")
        sys.exit(1)
    settings = Settings(token=token)
    app = Application.builder().token(token).build()
    app.bot_data["state"] = State(settings)
    only_private = private()
    commands = {"start": start, "status": status, "reset": reset, "history": history,
                "addglobalprompt": addglobalprompt, "addprivateprompt": addprivateprompt,
                "shutdown": shutdown, "help": help_command}
    for name, callback in commands.items():
        app.add_handler(CommandHandler(name, callback, filters=only_private))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Starting bot with llama.cpp model %s", settings.model)
    app.run_polling()


if __name__ == "__main__":
    main()
