"""Notification backends: one small interface, pluggable channels.

Adding a channel = new module + one line in :func:`build_notifiers`.
Secrets (bot tokens, webhook tokens) are read from environment variables only
and never sent to disk or into the report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .base import NotificationMessage, Notifier, NotifyResult
from .telegram import TelegramNotifier
from .webhook import WebhookNotifier

__all__ = [
    "NotificationMessage",
    "Notifier",
    "NotifyResult",
    "TelegramNotifier",
    "WebhookNotifier",
    "build_notifiers",
    "notify_all",
    "describe_notifiers",
]


def build_notifiers(cfg) -> list[Notifier]:  # noqa: ANN001 - Config, imported lazily to avoid a cycle
    """Instantiate every configured notification channel."""
    notifiers: list[Notifier] = []
    telegram = cfg.notify.telegram
    if telegram.enabled:
        notifiers.append(TelegramNotifier(bot_token_env=telegram.bot_token_env, chat_id=telegram.chat_id))
    webhook = cfg.notify.webhook
    if webhook.enabled:
        notifiers.append(WebhookNotifier(url=webhook.url, bearer_token_env=webhook.bearer_token_env))
    return notifiers


def notify_all(notifiers: list[Notifier], message: NotificationMessage) -> list[NotifyResult]:
    """Send to every channel; a failing channel never breaks the others."""
    results: list[NotifyResult] = []
    for notifier in notifiers:
        try:
            results.append(notifier.send(message))
        except Exception as exc:  # pragma: no cover - defensive
            results.append(NotifyResult(notifier=notifier.name, ok=False, detail=f"{type(exc).__name__}: {exc}"))
    return results


def describe_notifiers(notifiers: list[Notifier]) -> str:
    if not notifiers:
        return "none configured"
    return ", ".join(f"{n.name}{'' if n.ready else ' (missing credentials)'}" for n in notifiers)
