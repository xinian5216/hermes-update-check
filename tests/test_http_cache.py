"""HTTP layer tests: cache TTL, stale fallback, failure handling (no external network)."""

from __future__ import annotations

import time
from pathlib import Path

from hermes_update_check.errors import NetworkError, RateLimitError
from hermes_update_check.http import DiskCache, HttpClient, rate_limit_message, require_ok


def test_disk_cache_roundtrip(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_minutes=60)
    cache.set("key", {"hello": "world"})
    hit = cache.get("key")
    assert hit is not None
    payload, age = hit
    assert payload == {"hello": "world"}
    assert age >= 0


def test_disk_cache_expiry_and_stale(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_minutes=0)
    cache.set("key", {"v": 1})
    time.sleep(0.01)
    assert cache.get("key") is None  # expired at ttl=0
    stale = cache.get("key", allow_stale=True)
    assert stale is not None and stale[0] == {"v": 1}


def test_disk_cache_ignores_garbage(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_minutes=60)
    cache.set("k", 1)
    files = list(tmp_path.glob("*.json"))
    assert files
    files[0].write_text("{not json", encoding="utf-8")
    assert cache.get("k") is None


def test_cache_clear(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_minutes=60)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.clear() == 2


def test_zero_ttl_cache_is_never_fresh(tmp_path: Path) -> None:
    """Regression (found by CI on Windows): ttl_minutes=0 must mean "always refetch".

    The old ``age <= ttl`` comparison made a just-written entry look fresh when the
    filesystem timestamp resolution rounded the age to exactly 0.
    """
    cache = DiskCache(tmp_path, ttl_minutes=0)
    cache.set("k", {"cached": True})
    assert cache.get("k") is None  # not fresh
    assert cache.get("k", allow_stale=True) is not None  # but usable as a fallback


def test_http_client_reports_connection_failure() -> None:
    client = HttpClient(timeout=2, retries=0)
    result = client.get_json("http://127.0.0.1:1/nothing-here", use_cache=False)
    assert result.ok is False
    assert result.error
    assert result.degraded is True


def test_http_client_falls_back_to_stale_cache(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_minutes=0)
    cache.set("http://example.invalid/x", {"cached": True})
    client = HttpClient(timeout=2, retries=0, cache=cache)
    result = client.get_json("http://example.invalid/x")
    assert result.ok is True  # usable data is available...
    assert result.from_cache is True
    assert result.stale is True  # ...but it is a *stale* fallback
    assert result.degraded is True
    assert result.error  # the network failure is still reported
    assert result.data == {"cached": True}


def test_fresh_cache_hit_is_not_degraded(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_minutes=60)
    cache.set("http://example.invalid/y", {"cached": True})
    client = HttpClient(timeout=2, retries=0, cache=cache)
    result = client.get_json("http://example.invalid/y")
    assert result.ok is True
    assert result.from_cache is True
    assert result.stale is False
    assert result.error is None


def test_rate_limit_message_shapes_epoch() -> None:
    message = rate_limit_message("1789456164")
    assert "rate limit" in message.lower()
    assert "UTC" in message
    assert rate_limit_message(None)


def test_require_ok_raises_typed_errors() -> None:
    from hermes_update_check.http import HttpResult

    ok = HttpResult(url="u", status=200, data={"a": 1})
    assert require_ok(ok, what="thing") == {"a": 1}

    limited = HttpResult(url="u", status=403, error="GitHub API rate limit exhausted (resets at 12:00:00 UTC)")
    try:
        require_ok(limited, what="thing")
    except RateLimitError as exc:
        assert "rate limit" in exc.message.lower()
    else:  # pragma: no cover
        raise AssertionError("RateLimitError not raised")

    broken = HttpResult(url="u", error="connection failed: refused")
    try:
        require_ok(broken, what="thing")
    except NetworkError:
        pass
    else:  # pragma: no cover
        raise AssertionError("NetworkError not raised")


def test_fresh_request_skips_cache_read_but_writes_back(tmp_path: Path, monkeypatch) -> None:
    """fresh=True must re-fetch even when a fresh cached entry exists, and refresh it."""
    from hermes_update_check.http import HttpResult

    cache = DiskCache(tmp_path, ttl_minutes=60)
    cache.set("http://example.invalid/fresh", {"old": True})
    client = HttpClient(timeout=2, retries=0, cache=cache)
    calls = {"n": 0}

    def fake_request(url: str, headers) -> HttpResult:
        calls["n"] += 1
        return HttpResult(url=url, status=200, data={"new": True})

    monkeypatch.setattr(client, "_request_with_retries", fake_request)
    result = client.get_json("http://example.invalid/fresh", fresh=True)
    assert result.ok and result.data == {"new": True}
    assert result.from_cache is False
    assert calls["n"] == 1  # the fresh entry was NOT served from cache
    # ...but the fresh response still refreshed the cache for everyone else
    hit = cache.get("http://example.invalid/fresh")
    assert hit is not None and hit[0] == {"new": True}
    # a normal (non-fresh) call now serves from cache without hitting the network
    again = client.get_json("http://example.invalid/fresh")
    assert again.from_cache is True and again.data == {"new": True}
    assert calls["n"] == 1


def test_fresh_request_still_falls_back_to_stale_cache(tmp_path: Path, monkeypatch) -> None:
    """fresh means "don't trust fresh cache", not "die when the network dies"."""
    from hermes_update_check.http import HttpResult

    cache = DiskCache(tmp_path, ttl_minutes=0)  # nothing is ever fresh here
    cache.set("http://example.invalid/stale", {"old": True})
    client = HttpClient(timeout=2, retries=0, cache=cache)

    def failing_request(url: str, headers) -> HttpResult:
        return HttpResult(url=url, error="connection failed: test")

    monkeypatch.setattr(client, "_request_with_retries", failing_request)
    result = client.get_json("http://example.invalid/stale", fresh=True)
    assert result.ok is True
    assert result.from_cache is True and result.stale is True
    assert result.data == {"old": True}
