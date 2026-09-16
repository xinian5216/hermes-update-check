"""Telegram channel (first-class notifier - `my chat` is where alerts are read).

Setup:

1. talk to @BotFather -> create a bot -> copy the token;
2. put the token in the environment, e.g. in the Hermes ``.env``::

       HERMES_UPDATE_CHECK_TELEGRAM_TOKEN=123456:ABC...

3. send any message to your bot, then open
   ``https://api.telegram.org/bot<TOKEN>/getUpdates`` and copy ``result[].message.chat.id``;
4. put that id in ``config.yaml`` -> ``notify.telegram.chat_id``,
   and set ``notify.telegram.enabled: true``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import NotificationMessage, Notifier, NotifyResult

API_ROOT = "https://api.telegram.org"
MAX_LENGTH = 4096


@dataclass
class TelegramNotifier(Notifier):
    bot_token_env: str = "HERMES_UPDATE_CHECK_TELEGRAM_TOKEN"
    chat_id: str = ""
    timeout: float = 20.0
    name = "telegram"

    @property
    def token(self) -> Optional[str]:
        value = os.environ.get(self.bot_token_env, "").strip()
        return value or None

    @property
    def ready(self) -> bool:
        return bool(self.token and self.chat_id.strip())

    def send(self, message: NotificationMessage) -> NotifyResult:
        if not self.token:
            return NotifyResult(self.name, False, f"missing token in ${self.bot_token_env}")
        if not self.chat_id.strip():
            return NotifyResult(self.name, False, "missing notify.telegram.chat_id in config.yaml")

        text = message.to_text()
        if len(text) > MAX_LENGTH:
            text = text[: MAX_LENGTH - 20] + "\n...(truncated)"

        payload = {
            "chat_id": self.chat_id.strip(),
            "text": text,
            "disable_web_page_preview": True,
        }
        url = f"{API_ROOT}/bot{self.token}/sendMessage"
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # pragma: no cover
                body = ""
            return NotifyResult(self.name, False, f"HTTP {exc.code}: {body}")
        except (URLError, TimeoutError, OSError) as exc:
            return NotifyResult(self.name, False, f"connection failed: {exc}")

        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        if isinstance(data, dict) and data.get("ok"):
            return NotifyResult(self.name, True, "sent")
        return NotifyResult(self.name, False, f"telegram API error: {raw[:200]}")


def discover_chat_id(token: str, *, timeout: float = 15.0) -> list[dict[str, object]]:
    """Helper for setup: list chats that recently messaged the bot."""
    url = f"{API_ROOT}/bot{token}/getUpdates"
    try:
        with urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return [{"error": str(exc)}]
    out: list[dict[str, object]] = []
    for update in (data.get("result") or [])[-20:]:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            out.append(
                {
                    "chat_id": chat.get("id"),
                    "type": chat.get("type"),
                    "title": chat.get("title") or chat.get("username") or chat.get("first_name"),
                }
            )
    return out
