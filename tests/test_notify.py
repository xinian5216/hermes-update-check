"""Notification-channel tests: payload shape, failure handling, no secret leakage.

The channels talk HTTP through ``urlopen``; every test injects a stub instead of
touching the network, and the token-handling tests assert that a configured
secret never ends up in the result text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_update_check.config import Config
from hermes_update_check.notify import (
    NotificationMessage,
    NotifyResult,
    TelegramNotifier,
    WebhookNotifier,
    build_notifiers,
    describe_notifiers,
    notify_all,
)

FAKE_TOKEN = "123456789:" + "AA" + "fake-token-for-tests-only-0123456789"
FAKE_SECRET = "super-secret-bearer-value"


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"ok": true}') -> None:
        self.status = status
        self._body = body
        self.read_called = False

    def read(self) -> bytes:
        self.read_called = True
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def make_message() -> NotificationMessage:
    return NotificationMessage(
        title="Hermes 更新建议：暂缓",
        body="风险 92/100 VERY HIGH",
        url="https://example.invalid/report",
        level="WAIT",
    )


# --------------------------------------------------------------------------- #
# message model
# --------------------------------------------------------------------------- #
def test_message_to_dict_and_text() -> None:
    message = make_message()
    data = message.to_dict()
    assert data["title"] == "Hermes 更新建议：暂缓"
    assert "92/100" in message.to_text()
    assert "https://example.invalid/report" in message.to_text()


# --------------------------------------------------------------------------- #
# webhook
# --------------------------------------------------------------------------- #
def test_webhook_not_ready_without_url() -> None:
    result = WebhookNotifier(url="").send(make_message())
    assert result.ok is False
    assert "no webhook url" in result.detail


def test_webhook_sends_json_and_attaches_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse(200)

    monkeypatch.setattr("hermes_update_check.notify.webhook.urlopen", fake_urlopen)
    monkeypatch.setenv("HERMES_UPDATE_CHECK_WEBHOOK_TOKEN", FAKE_SECRET)

    result = WebhookNotifier(url="https://hooks.example.invalid/abc").send(make_message())

    assert result.ok is True
    assert captured["body"]["title"] == "Hermes 更新建议：暂缓"
    assert captured["headers"]["authorization"] == f"Bearer {FAKE_SECRET}"
    assert FAKE_SECRET not in result.detail  # the secret stays out of the result


def test_webhook_without_token_sends_no_authorization_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        return FakeResponse(200)

    monkeypatch.setattr("hermes_update_check.notify.webhook.urlopen", fake_urlopen)
    monkeypatch.delenv("HERMES_UPDATE_CHECK_WEBHOOK_TOKEN", raising=False)

    assert WebhookNotifier(url="https://hooks.example.invalid/x").send(make_message()).ok is True
    assert "authorization" not in captured["headers"]


@pytest.mark.parametrize(
    "error,expected",
    [
        (__import__("urllib.error", fromlist=["HTTPError"]).HTTPError("u", 500, "boom", {}, None), "HTTP 500"),
        (__import__("urllib.error", fromlist=["URLError"]).URLError("dns"), "connection failed"),
        (TimeoutError("slow"), "connection failed"),
    ],
)
def test_webhook_reports_transport_failures(monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str) -> None:
    def fake_urlopen(request, timeout=None):
        raise error

    monkeypatch.setattr("hermes_update_check.notify.webhook.urlopen", fake_urlopen)
    result = WebhookNotifier(url="https://hooks.example.invalid/x").send(make_message())
    assert result.ok is False
    assert expected in result.detail


def test_webhook_flags_an_unexpected_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hermes_update_check.notify.webhook.urlopen", lambda request, timeout=None: FakeResponse(302))
    result = WebhookNotifier(url="https://hooks.example.invalid/x").send(make_message())
    assert result.ok is False
    assert "unexpected HTTP 302" in result.detail


# --------------------------------------------------------------------------- #
# telegram
# --------------------------------------------------------------------------- #
def test_telegram_reports_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_UPDATE_CHECK_TELEGRAM_TOKEN", raising=False)
    assert TelegramNotifier(chat_id="42").send(make_message()).ok is False
    assert "missing token" in TelegramNotifier(chat_id="42").send(make_message()).detail

    monkeypatch.setenv("HERMES_UPDATE_CHECK_TELEGRAM_TOKEN", FAKE_TOKEN)
    result = TelegramNotifier(chat_id="").send(make_message())
    assert result.ok is False
    assert "chat_id" in result.detail


def test_telegram_ready_property(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_UPDATE_CHECK_TELEGRAM_TOKEN", raising=False)
    assert TelegramNotifier(chat_id="42").ready is False
    monkeypatch.setenv("HERMES_UPDATE_CHECK_TELEGRAM_TOKEN", FAKE_TOKEN)
    assert TelegramNotifier(chat_id="42").ready is True
    assert TelegramNotifier(chat_id="  ").ready is False


def test_telegram_posts_to_the_bot_api_without_leaking_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(200, b'{"ok": true}')

    monkeypatch.setattr("hermes_update_check.notify.telegram.urlopen", fake_urlopen)
    monkeypatch.setenv("HERMES_UPDATE_CHECK_TELEGRAM_TOKEN", FAKE_TOKEN)

    result = TelegramNotifier(chat_id="4242").send(make_message())

    assert result.ok is True
    assert FAKE_TOKEN in captured["url"]  # the token belongs in the URL path
    assert FAKE_TOKEN not in result.detail  # but never in what we report back
    assert captured["payload"]["chat_id"] == "4242"
    assert "92/100" in captured["payload"]["text"]


def test_telegram_truncates_an_overlong_message(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout=None):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(200)

    monkeypatch.setattr("hermes_update_check.notify.telegram.urlopen", fake_urlopen)
    monkeypatch.setenv("HERMES_UPDATE_CHECK_TELEGRAM_TOKEN", FAKE_TOKEN)

    huge = NotificationMessage(title="t", body="x" * 9000)
    assert TelegramNotifier(chat_id="1").send(huge).ok is True
    assert len(captured["payload"]["text"]) <= 4096
    assert captured["payload"]["text"].endswith("...(truncated)")


def test_telegram_reports_http_and_transport_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    monkeypatch.setenv("HERMES_UPDATE_CHECK_TELEGRAM_TOKEN", FAKE_TOKEN)

    def http_error(request, timeout=None):
        raise urllib.error.HTTPError("u", 401, "unauthorized", {}, None)

    monkeypatch.setattr("hermes_update_check.notify.telegram.urlopen", http_error)
    failed = TelegramNotifier(chat_id="1").send(make_message())
    assert failed.ok is False
    assert "401" in failed.detail

    def transport_error(request, timeout=None):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("hermes_update_check.notify.telegram.urlopen", transport_error)
    assert TelegramNotifier(chat_id="1").send(make_message()).ok is False


# --------------------------------------------------------------------------- #
# registry / fan-out
# --------------------------------------------------------------------------- #
def test_build_notifiers_respects_the_config(cfg: Config, hermes_home: Path) -> None:
    assert build_notifiers(cfg) == []  # nothing enabled by default

    cfg.notify.telegram.enabled = True
    cfg.notify.telegram.chat_id = "42"
    cfg.notify.webhook.enabled = True
    cfg.notify.webhook.url = "https://hooks.example.invalid/x"

    notifiers = build_notifiers(cfg)
    assert [n.name for n in notifiers] == ["telegram", "webhook"]
    assert describe_notifiers(notifiers).startswith("telegram")


def test_describe_notifiers_marks_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_UPDATE_CHECK_TELEGRAM_TOKEN", raising=False)
    assert describe_notifiers([]) == "none configured"
    text = describe_notifiers([TelegramNotifier(chat_id="42")])
    assert "missing credentials" in text


def test_notify_all_isolates_a_failing_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    class Exploding:
        name = "boom"
        ready = True

        def send(self, message: NotificationMessage) -> NotifyResult:
            raise RuntimeError("channel is broken")

    class Quiet:
        name = "quiet"
        ready = True

        def send(self, message: NotificationMessage) -> NotifyResult:
            return NotifyResult(notifier="quiet", ok=True, detail="sent")

    results = notify_all([Exploding(), Quiet()], make_message())

    assert [r.ok for r in results] == [False, True]
    assert "RuntimeError" in results[0].detail
    assert results[0].notifier == "boom"
