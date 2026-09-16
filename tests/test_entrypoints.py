"""Smoke tests for the two entry points that had no direct coverage.

`python -m hermes_update_check` (``__main__``) and ``logging_setup`` were only
exercised indirectly; both are part of the public surface (the README advertises
the module form, and every module logs through the configured logger).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from hermes_update_check.logging_setup import LOGGER_NAME, get_logger, setup_logging


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hermes_update_check", *args],
        capture_output=True,
        text=True,
    )


def test_module_entrypoint_prints_the_version() -> None:
    result = _run("version")
    assert result.returncode == 0, result.stderr
    assert "hermes-update-check" in result.stdout


def test_module_entrypoint_shows_help() -> None:
    result = _run("--help")
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()


def test_help_survives_a_legacy_console_encoding() -> None:
    """Regression (found by the Windows CI job): cp1252 consoles cannot encode the
    Chinese help text, and printing it raised UnicodeEncodeError -> exit 1."""
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    result = subprocess.run(
        [sys.executable, "-m", "hermes_update_check", "--help"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()


def test_json_output_survives_a_legacy_console_encoding(tmp_path: Path) -> None:
    """Machine-readable output must stay parseable regardless of console locale."""
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    result = subprocess.run(
        [sys.executable, "-m", "hermes_update_check", "--json", "config", "show"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    json.loads(result.stdout)


def test_module_entrypoint_rejects_unknown_command() -> None:
    result = _run("definitely-not-a-command")
    assert result.returncode != 0


def test_setup_logging_writes_a_log_file(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "hermes-update-check.log"
    logger = setup_logging(level="DEBUG", log_file=log_file, console=False)

    logger.debug("hello from the test")
    for handler in logger.handlers:
        handler.flush()

    assert log_file.exists()
    assert "hello from the test" in log_file.read_text(encoding="utf-8")
    assert logger.level == logging.DEBUG


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    """Calling it twice must not stack handlers (that would duplicate log lines)."""
    first = setup_logging(level="INFO", log_file=tmp_path / "a.log")
    second = setup_logging(level="INFO", log_file=tmp_path / "b.log")

    assert first is second
    file_handlers = [h for h in second.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1


def test_setup_logging_never_breaks_on_an_unwritable_path(tmp_path: Path) -> None:
    """A broken log path is not worth failing a risky-update check over."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    logger = setup_logging(level="INFO", log_file=blocker / "nested" / "x.log")

    assert logger is not None
    assert logger.handlers  # a NullHandler at worst


def test_unknown_level_falls_back_to_info(tmp_path: Path) -> None:
    logger = setup_logging(level="not-a-level", log_file=tmp_path / "c.log")
    assert logger.level == logging.INFO


def test_get_logger_works_before_setup() -> None:
    logger = get_logger("sometest")
    assert logger.name == f"{LOGGER_NAME}.sometest"
    assert get_logger().name == LOGGER_NAME
