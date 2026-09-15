"""Terminal rendering: pretty with `rich`, correct with plain text.

`rich` is a declared dependency, but the tool must keep working (and stay
readable) on a stripped-down VPS where the import fails - so every call here
has a plain-text path. `--plain` / ``NO_COLOR`` / a non-tty stdout force the
plain renderer.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Sequence

try:  # pragma: no cover - environment dependent
    from rich.console import Console as _RichConsole
    from rich.markup import escape as _rich_escape
    from rich.panel import Panel as _RichPanel
    from rich.table import Table as _RichTable
    from rich.text import Text as _RichText

    _RICH_AVAILABLE = True
except Exception:  # pragma: no cover - environment dependent
    _RichConsole = None  # type: ignore[assignment]
    _rich_escape = None  # type: ignore[assignment]
    _RichPanel = None  # type: ignore[assignment]
    _RichTable = None  # type: ignore[assignment]
    _RichText = None  # type: ignore[assignment]
    _RICH_AVAILABLE = False


LEVEL_STYLES: dict[str, str] = {
    "LOW": "bold green",
    "LOW-MEDIUM": "bold cyan",
    "MEDIUM": "bold yellow",
    "HIGH": "bold red",
    "VERY HIGH": "bold white on red",
    "UNKNOWN": "bold magenta",
    "PASS": "green",
    "WARN": "yellow",
    "FAIL": "bold red",
    "INFO": "cyan",
    "OK": "green",
    "UPDATE": "bold green",
    "WAIT": "bold yellow",
    "AVOID": "bold red",
    "SKIP": "dim",
}

PLAIN_MARKERS: dict[str, str] = {
    "LOW": "[LOW]",
    "LOW-MEDIUM": "[LOW-MED]",
    "MEDIUM": "[MEDIUM]",
    "HIGH": "[HIGH]",
    "VERY HIGH": "[VERY HIGH]",
    "UNKNOWN": "[UNKNOWN]",
    "PASS": "[PASS]",
    "WARN": "[WARN]",
    "FAIL": "[FAIL]",
    "INFO": "[INFO]",
    "OK": "[ OK ]",
    "UPDATE": "[UPDATE]",
    "WAIT": "[WAIT]",
    "AVOID": "[AVOID]",
    "SKIP": "[SKIP]",
}


def rich_available() -> bool:
    return _RICH_AVAILABLE


@dataclass
class Console:
    """Minimal rendering facade used by every command."""

    plain: bool = False
    no_color: bool = False
    quiet: bool = False

    def __post_init__(self) -> None:
        if os.environ.get("NO_COLOR"):
            self.no_color = True
        if self.plain:
            self._rich = None
        else:
            self._rich = _RichConsole(highlight=False, soft_wrap=True) if _RICH_AVAILABLE else None
            if self._rich is not None and self.no_color:
                self._rich.no_color = True

    # -- primitives ---------------------------------------------------------- #

    def print(self, text: str = "") -> None:
        if self.quiet:
            return
        if self._rich is not None:
            self._rich.print(text, markup=False, highlight=False)
        else:
            sys.stdout.write(text + "\n")

    def print_markup(self, markup: str) -> None:
        """Print a rich-markup string; falls back to stripped text."""
        if self.quiet:
            return
        if self._rich is not None:
            self._rich.print(markup)
        else:
            sys.stdout.write(_strip_markup(markup) + "\n")

    def heading(self, title: str) -> None:
        if self.quiet:
            return
        if self._rich is not None:
            self._rich.rule(f"[bold]{_escape(title)}[/bold]", align="left")
        else:
            sys.stdout.write(f"\n== {title} " + "=" * max(0, 58 - len(title)) + "\n")

    def blank(self) -> None:
        self.print("")

    def badge(self, level: str) -> str:
        """Coloured level label (or a plain marker when rich is unavailable)."""
        key = level.upper()
        if self.plain or self._rich is None:
            return PLAIN_MARKERS.get(key, f"[{key}]")
        style = LEVEL_STYLES.get(key, "bold")
        return f"[{style}]{_escape(key)}[/{style}]"

    # -- composites ---------------------------------------------------------- #

    def kv_table(self, rows: Sequence[tuple[str, str]], *, title: str | None = None) -> None:
        if self.quiet:
            return
        if self._rich is not None:
            table = _RichTable(show_header=False, box=None, padding=(0, 2), title=title)
            table.add_column(style="dim", no_wrap=True)
            table.add_column()
            for key, value in rows:
                table.add_row(_escape(str(key)), _escape(str(value)))
            self._rich.print(table)
        else:
            if title:
                sys.stdout.write(f"{title}\n")
            width = max((len(str(k)) for k, _ in rows), default=0)
            for key, value in rows:
                sys.stdout.write(f"  {str(key).ljust(width)}  {value}\n")

    def panel(self, body: str, *, title: str | None = None, level: str = "INFO") -> None:
        if self.quiet:
            return
        if self._rich is not None:
            style = LEVEL_STYLES.get(level.upper(), "white")
            self._rich.print(
                _RichPanel(
                    _rich_escape(body) if _rich_escape else body,
                    title=_escape(title) if title else None,
                    border_style=style.split()[-1] if style else "white",
                    expand=False,
                )
            )
        else:
            bar = "-" * 62
            if title:
                sys.stdout.write(f"{bar}\n{title}\n{bar}\n")
            sys.stdout.write(body.rstrip() + "\n")
            sys.stdout.write(bar + "\n")

    def bullets(self, items: Sequence[str], *, marker: str = "-") -> None:
        for item in items:
            self.print(f"  {marker} {item}")

    def numbered(self, items: Sequence[str]) -> None:
        for idx, item in enumerate(items, 1):
            self.print(f"  {idx}. {item}")

    def warn(self, message: str) -> None:
        self.print_markup(f"{self.badge('WARN')} {_escape(message)}")

    def error(self, message: str) -> None:
        if self._rich is not None and not self.plain:
            self._rich.print(f"[bold red]error:[/bold red] {_escape(message)}", markup=True)
        else:
            sys.stderr.write(f"error: {message}\n")

    def info(self, message: str) -> None:
        self.print_markup(f"{self.badge('INFO')} {_escape(message)}")

    def success(self, message: str) -> None:
        self.print_markup(f"{self.badge('OK')} {_escape(message)}")

    def status_line(self, name: str, status: str, detail: str = "") -> None:
        badge = self.badge(status)
        suffix = f"  {_escape(detail)}" if detail else ""
        self.print_markup(f"  {badge} {_escape(name)}{suffix}")


def _escape(text: str) -> str:
    """Escape rich markup in user-controlled strings (version bodies, paths...)."""
    if _rich_escape is None:
        return text
    return _rich_escape(str(text))


def _strip_markup(markup: str) -> str:
    out: list[str] = []
    depth = 0
    idx = 0
    while idx < len(markup):
        char = markup[idx]
        if char == "[":
            close = markup.find("]", idx)
            if close == -1:
                out.append(char)
                idx += 1
                continue
            inner = markup[idx + 1 : close]
            if inner.startswith("/"):
                depth = max(0, depth - 1)
            elif inner and not inner[0].isdigit() and " " not in inner and "=" not in inner:
                depth += 1
            else:
                out.append(char)
            idx = close + 1
            continue
        out.append(char)
        idx += 1
    return "".join(out)
