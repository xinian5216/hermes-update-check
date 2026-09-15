"""Generic JSON webhook channel (Slack/Discord/n8n/your own endpoint)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import NotificationMessage, Notifier, NotifyResult


@dataclass
class WebhookNotifier(Notifier):
    url: str = ""
    bearer_token_env: str = "HERMES_UPDATE_CHECK_WEBHOOK_TOKEN"
    timeout: float = 20.0
    name = "webhook"

    @property
    def ready(self) -> bool:
        return bool(self.url.strip())

    def send(self, message: NotificationMessage) -> NotifyResult:
        if not self.ready:
            return NotifyResult(self.name, False, "no webhook url configured")

        headers = {"Content-Type": "application/json", "User-Agent": "hermes-update-check"}
        token = os.environ.get(self.bearer_token_env, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        request = Request(
            self.url.strip(),
            data=json.dumps(message.to_dict()).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - user-supplied URL by design
                status = getattr(response, "status", 200)
                response.read()
        except HTTPError as exc:
            return NotifyResult(self.name, False, f"HTTP {exc.code}")
        except (URLError, TimeoutError, OSError) as exc:
            return NotifyResult(self.name, False, f"connection failed: {exc}")
        if 200 <= int(status) < 300:
            return NotifyResult(self.name, True, f"sent (HTTP {status})")
        return NotifyResult(self.name, False, f"unexpected HTTP {status}")
