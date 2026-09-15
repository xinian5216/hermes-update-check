"""HTTP layer: stdlib-only client with timeouts, retries, on-disk cache and rate-limit awareness.

Why not `requests`? A VPS that only has stock python3 + pip is the common case
for this tool, and urllib is enough for a handful of GET requests. The cache
matters for two reasons:

* cron/watch mode must not burn a 60 req/h quota on every run;
* when the network is down the last known release data is still useful - but it
  is surfaced as *degraded*, never silently as fresh.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import TOOL_NAME, __version__
from .errors import NetworkError, RateLimitError
from .logging_setup import get_logger
from .util import ensure_dir, read_json, utcnow, write_json

USER_AGENT = f"{TOOL_NAME}/{__version__} (+https://github.com/NousResearch/hermes-agent)"

#: Circuit breaker: after this many consecutive transport failures we stop
#: hammering a dead network inside a single run.
MAX_CONSECUTIVE_FAILURES = 4


@dataclass
class HttpResult:
    """Outcome of one HTTP GET, including cache provenance."""

    url: str
    status: int = 0
    data: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False
    cache_age_seconds: Optional[float] = None
    error: Optional[str] = None
    attempts: int = 1
    stale: bool = False

    @property
    def ok(self) -> bool:
        """True when *usable data* is available - including a stale-cache fallback.

        A stale fallback still carries ``error`` (why the network call failed)
        and ``stale``/``degraded`` flags so the report can label the data.
        """
        return self.data is not None

    @property
    def degraded(self) -> bool:
        return self.stale or self.from_cache or self.error is not None

    def header(self, name: str) -> Optional[str]:
        return self.headers.get(name.lower())

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "from_cache": self.from_cache,
            "stale": self.stale,
            "cache_age_seconds": self.cache_age_seconds,
            "error": self.error,
            "attempts": self.attempts,
        }


class DiskCache:
    """Tiny JSON cache: one file per cache key, TTL checked on read."""

    def __init__(self, directory: Path, ttl_minutes: int = 360, *, logger: Optional[logging.Logger] = None) -> None:
        self.directory = directory
        self.ttl_seconds = max(0, int(ttl_minutes)) * 60
        self.log = logger or get_logger("cache")

    def _path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        return self.directory / f"{digest}.json"

    def get(self, key: str, *, allow_stale: bool = False, max_stale_seconds: float = 7 * 86400) -> Optional[tuple[Any, float]]:
        """Return ``(payload, age_seconds)`` or None.

        ``allow_stale`` is the offline fallback: keep using old data, but the
        caller marks the result as degraded.
        """
        path = self._path(key)
        payload = read_json(path)
        if not isinstance(payload, dict) or "saved_at" not in payload:
            return None
        try:
            saved_at = float(payload["saved_at"])
        except (TypeError, ValueError):
            return None
        age = time.time() - saved_at
        fresh = age <= self.ttl_seconds
        if not fresh and not (allow_stale and age <= max_stale_seconds):
            return None
        return payload.get("data"), age

    def set(self, key: str, data: Any) -> None:
        try:
            ensure_dir(self.directory)
            write_json(self._path(key), {"saved_at": time.time(), "saved_at_iso": utcnow().isoformat(), "data": data})
        except OSError as exc:  # cache failures must never be fatal
            self.log.debug("cache write failed for %s: %s", key, exc)

    def clear(self) -> int:
        removed = 0
        if not self.directory.exists():
            return 0
        for path in self.directory.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed


class HttpClient:
    """Blocking JSON GET client with retry, cache and rate-limit handling."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        retries: int = 2,
        cache: Optional[DiskCache] = None,
        token: Optional[str] = None,
        user_agent: str = USER_AGENT,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.timeout = max(1.0, float(timeout))
        self.retries = max(0, int(retries))
        self.cache = cache
        self.token = token
        self.user_agent = user_agent
        self.log = logger or get_logger("http")
        self._consecutive_failures = 0

    # -- public API ---------------------------------------------------------- #

    def get_json(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        use_cache: bool = True,
        cache_key: Optional[str] = None,
    ) -> HttpResult:
        """GET a JSON document. Never raises for network problems: inspect ``result.error``."""
        full_url = url
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                full_url = f"{url}?{urlencode(filtered)}"
        key = cache_key or full_url

        if use_cache and self.cache is not None:
            fresh = self.cache.get(key)
            if fresh is not None:
                payload, age = fresh
                self.log.debug("cache hit %s (age %.0fs)", key, age)
                return HttpResult(url=full_url, status=200, data=payload, from_cache=True, cache_age_seconds=age)

        result = self._request_with_retries(full_url, extra_headers)

        if result.ok and use_cache and self.cache is not None:
            self.cache.set(key, result.data)

        if not result.ok and self.cache is not None:
            stale = self.cache.get(key, allow_stale=True)
            if stale is not None:
                payload, age = stale
                self.log.warning("network failed (%s); using stale cache (age %.0fs)", result.error, age)
                result.data = payload
                result.from_cache = True
                result.stale = True
                result.cache_age_seconds = age
                result.status = result.status or 0
        return result

    def probe_rate_limit(self, url: str = "https://api.github.com/rate_limit") -> dict[str, Any]:
        """Best-effort rate limit snapshot for the report footer."""
        result = self.get_json(url, use_cache=False)
        if result.ok and isinstance(result.data, dict):
            return result.data  # type: ignore[return-value]
        return {}

    # -- internals ----------------------------------------------------------- #

    def _request_with_retries(self, url: str, extra_headers: Optional[Mapping[str, str]]) -> HttpResult:
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            return HttpResult(url=url, error="network circuit breaker open (too many failures this run)")

        last = HttpResult(url=url, error="no attempt made")
        attempts = max(1, self.retries + 1)
        for attempt in range(1, attempts + 1):
            last = self._request_once(url, extra_headers)
            last.attempts = attempt
            if last.ok:
                self._consecutive_failures = 0
                return last
            if last.status in (403, 429) or (last.status and 400 <= last.status < 500):
                # Client errors and rate limits do not improve by retrying.
                break
            if attempt < attempts:
                backoff = 0.5 * (2 ** (attempt - 1))
                self.log.debug("retrying %s in %.1fs (attempt %d/%d)", url, backoff, attempt, attempts)
                time.sleep(backoff)

        self._consecutive_failures += 1
        return last

    def _request_once(self, url: str, extra_headers: Optional[Mapping[str, str]]) -> HttpResult:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra_headers:
            headers.update({str(k): str(v) for k, v in extra_headers.items()})

        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - https only, caller-controlled
                raw = response.read()
                parsed = _decode_json(raw)
                return HttpResult(
                    url=url,
                    status=int(getattr(response, "status", 200)),
                    data=parsed,
                    headers={k.lower(): v for k, v in response.headers.items()},
                )
        except HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # pragma: no cover - defensive
                body = ""
            headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            if exc.code in (403, 429):
                remaining = headers.get("x-ratelimit-remaining")
                reset = headers.get("x-ratelimit-reset")
                if remaining == "0" or "rate limit" in body.lower():
                    return HttpResult(
                        url=url,
                        status=exc.code,
                        headers=headers,
                        error=rate_limit_message(reset),
                    )
            return HttpResult(url=url, status=exc.code, headers=headers, error=f"HTTP {exc.code}: {body or exc.reason}")
        except URLError as exc:
            return HttpResult(url=url, error=f"connection failed: {exc.reason}")
        except TimeoutError:
            return HttpResult(url=url, error=f"timeout after {self.timeout:g}s")
        except OSError as exc:
            return HttpResult(url=url, error=f"OS error: {exc}")
        except Exception as exc:  # pragma: no cover - never let HTTP kill a run
            return HttpResult(url=url, error=f"unexpected error: {exc}")


def rate_limit_message(reset_header: Optional[str]) -> str:
    when = "unknown"
    if reset_header:
        try:
            from datetime import datetime, timezone

            when = datetime.fromtimestamp(int(reset_header), tz=timezone.utc).strftime("%H:%M:%S UTC")
        except (TypeError, ValueError):
            when = "unknown"
    return f"GitHub API rate limit exhausted (resets at {when}); set GITHUB_TOKEN for 5000 req/h"


def _decode_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None


def require_ok(result: HttpResult, *, what: str) -> Any:
    """Turn an HttpResult into data or raise the right typed error."""
    if result.ok:
        return result.data
    if result.error and "rate limit" in result.error.lower():
        raise RateLimitError(result.error, status=result.status, url=result.url)
    raise NetworkError(f"{what} failed: {result.error or 'unknown error'}", hint="check connectivity / proxy settings")
