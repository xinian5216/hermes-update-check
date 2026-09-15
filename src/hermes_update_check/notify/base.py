"""Notifier interface and the message object every channel receives."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class NotificationMessage:
    """Channel-neutral notification payload."""

    title: str
    body: str
    level: str = "INFO"  # INFO | LOW | MEDIUM | HIGH | VERY HIGH | UNKNOWN
    tag: Optional[str] = None
    url: Optional[str] = None
    fields: dict[str, str] = field(default_factory=dict)

    def to_text(self) -> str:
        lines = [self.title, ""]
        if self.tag:
            lines.append(f"release: {self.tag}")
        if self.level:
            lines.append(f"risk: {self.level}")
        lines.append("")
        lines.append(self.body.strip())
        if self.url:
            lines.append("")
            lines.append(self.url)
        if self.fields:
            lines.append("")
            for key, value in self.fields.items():
                lines.append(f"{key}: {value}")
        return "\n".join(line for line in lines if line is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "level": self.level,
            "tag": self.tag,
            "url": self.url,
            "fields": dict(self.fields),
        }


@dataclass
class NotifyResult:
    notifier: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"notifier": self.notifier, "ok": self.ok, "detail": self.detail}


class Notifier:
    """Base class: implement ``name``, ``ready`` and ``send``."""

    name = "base"

    @property
    def ready(self) -> bool:
        raise NotImplementedError

    def send(self, message: NotificationMessage) -> NotifyResult:
        raise NotImplementedError
