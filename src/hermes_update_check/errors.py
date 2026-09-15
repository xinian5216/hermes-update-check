"""Exceptions and process exit codes.

Exit codes are part of the public contract (cron jobs, CI, shell wrappers):

===== ==========================================================
 code  meaning
===== ==========================================================
   0   success (check completed: either up to date, or update is safe)
   1   unexpected runtime error
   2   usage error (bad arguments)
   3   configuration error
  10   check completed, but the recommendation is WAIT / AVOID
  11   INSUFFICIENT DATA - the risk could not be assessed
  12   post-update health check FAILED
  13   aborted by the user / confirmation refused
  14   preflight check FAILED (update not started)
===== ==========================================================
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_WAIT = 10
EXIT_INSUFFICIENT_DATA = 11
EXIT_HEALTH_FAILED = 12
EXIT_ABORTED = 13
EXIT_PREFLIGHT_FAILED = 14


class HermesUpdateCheckError(Exception):
    """Base class for every error raised by this tool.

    ``exit_code`` lets the CLI translate the error into a stable exit status
    without a giant except-chain.
    """

    exit_code = EXIT_ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.hint:
            return f"{self.message}\n  hint: {self.hint}"
        return self.message


class ConfigError(HermesUpdateCheckError):
    """Invalid or unreadable configuration."""

    exit_code = EXIT_CONFIG


class NetworkError(HermesUpdateCheckError):
    """A network operation failed (timeout, DNS, TLS, HTTP 5xx...)."""


class GitHubError(NetworkError):
    """GitHub API returned an unexpected response."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status = status
        self.url = url


class RateLimitError(GitHubError):
    """GitHub rate limit exhausted (HTTP 403/429 with rate-limit headers)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        reset_at: float | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, status=status, url=url, hint=hint)
        self.reset_at = reset_at


class CommandError(HermesUpdateCheckError):
    """An external command (hermes, git, uv, ...) failed."""


class PreflightError(HermesUpdateCheckError):
    """A blocking pre-update check failed."""

    exit_code = EXIT_PREFLIGHT_FAILED


class HealthCheckFailed(HermesUpdateCheckError):
    """The post-update health check failed."""

    exit_code = EXIT_HEALTH_FAILED


class AbortedError(HermesUpdateCheckError):
    """The user (or a non-interactive stdin) refused to continue."""

    exit_code = EXIT_ABORTED


class InsufficientDataError(HermesUpdateCheckError):
    """Not enough information to produce a trustworthy risk assessment."""

    exit_code = EXIT_INSUFFICIENT_DATA
