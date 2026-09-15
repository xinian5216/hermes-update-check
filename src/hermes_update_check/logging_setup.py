"""Logging setup: file + optional console, quiet by default on stdout.

The report owns stdout. Logs go to ``<state_dir>/logs/hermes-update-check.log``
(rotating, 2 MiB x 3) and, only when explicitly enabled, to stderr.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOGGER_NAME = "hermes_update_check"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 3


def setup_logging(
    *,
    level: str = "INFO",
    log_file: Path | None = None,
    console: bool = False,
) -> logging.Logger:
    """Configure the package logger exactly once and return it."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_resolve_level(level))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - best effort
            pass

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                str(log_file), maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError:
            # An unwritable log file must never break the tool.
            pass

    if console:
        stream_handler = logging.StreamHandler(stream=sys.stderr)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Child logger; works even before setup_logging() was called."""
    if name:
        return logging.getLogger(f"{LOGGER_NAME}.{name}")
    return logging.getLogger(LOGGER_NAME)


def _resolve_level(level: str) -> int:
    value = getattr(logging, str(level).upper(), None)
    return value if isinstance(value, int) else logging.INFO
