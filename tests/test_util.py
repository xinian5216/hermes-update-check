"""Unit tests for the shared helpers - the layer every other module leans on."""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from hermes_update_check.util import (
    Degradation,
    ProcResult,
    clamp,
    days_ago,
    dir_size_bytes,
    disk_free_bytes,
    ensure_dir,
    format_local,
    hours_between,
    human_bytes,
    humanize_hours,
    iso,
    later_of,
    parse_iso8601,
    platform_summary,
    read_json,
    resolve_executable,
    run_process,
    sha256_file,
    suppress_oserror,
    unique_preserve_order,
    utcnow,
    wrap_command,
    write_json,
    write_text,
)


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #
def test_utcnow_is_timezone_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "value,expected_year",
    [
        ("2026-09-14T16:04:14Z", 2026),
        ("2026-09-14T16:04:14+00:00", 2026),
        ("2026-09-14", 2026),
    ],
)
def test_parse_iso8601_accepts_common_forms(value: str, expected_year: int) -> None:
    parsed = parse_iso8601(value)
    assert parsed is not None
    assert parsed.year == expected_year
    assert parsed.tzinfo is not None


@pytest.mark.parametrize("value", [None, "", "not a date", "2026-13-45T99:99:99Z"])
def test_parse_iso8601_returns_none_for_garbage(value: str | None) -> None:
    assert parse_iso8601(value) is None


def test_hours_between_and_later_of() -> None:
    now = utcnow()
    earlier = now - timedelta(hours=3)
    assert hours_between(earlier, now) == pytest.approx(3.0, abs=0.01)
    assert hours_between(earlier) == pytest.approx(3.0, abs=0.01)
    assert hours_between(now, now - timedelta(hours=1)) < 0  # negative for inverted ranges
    latest = later_of(None, earlier, now)
    assert latest == now
    assert later_of(None, None) is None


def test_humanize_hours_in_both_languages() -> None:
    assert "小时" in humanize_hours(5.0)
    assert "天" in humanize_hours(60.0)
    assert "hour" in humanize_hours(5.0, lang="en")
    assert "day" in humanize_hours(60.0, lang="en")


def test_days_ago_is_before_now() -> None:
    moment = days_ago(2.0)
    assert utcnow() - moment >= timedelta(days=1, hours=23)


def test_iso_and_format_local_handle_none() -> None:
    assert iso(None) is None
    assert format_local(None) == "-"
    stamp = utcnow()
    assert iso(stamp) is not None and iso(stamp).endswith("Z")
    assert format_local(stamp)


# --------------------------------------------------------------------------- #
# file helpers
# --------------------------------------------------------------------------- #
def test_json_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "data.json"
    write_json(path, {"b": 1, "a": [1, 2]})
    assert read_json(path) == {"b": 1, "a": [1, 2]}
    assert json.loads(path.read_text(encoding="utf-8"))["b"] == 1


def test_read_json_tolerates_missing_and_garbage(tmp_path: Path) -> None:
    assert read_json(tmp_path / "absent.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert read_json(broken) is None


def test_write_text_creates_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c.txt"
    write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_ensure_dir_is_idempotent(tmp_path: Path) -> None:
    first = ensure_dir(tmp_path / "x")
    second = ensure_dir(tmp_path / "x")
    assert first == second and first.is_dir()


def test_suppress_oserror_swallows_only_oserror(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")

    with suppress_oserror():
        ensure_dir(blocker / "impossible")  # would raise NotADirectoryError

    with suppress_oserror():
        raise FileNotFoundError("swallowed too")

    with pytest.raises(ValueError):  # anything that is not an OSError still propagates
        with suppress_oserror():
            raise ValueError("boom")


def test_sha256_file_and_missing_path(tmp_path: Path) -> None:
    target = tmp_path / "payload.bin"
    target.write_bytes(b"abc")
    # known digest of b"abc"
    assert sha256_file(target) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert sha256_file(tmp_path / "nope.bin") is None
    assert sha256_file(tmp_path) is None  # a directory is not a file


def test_dir_size_bytes_and_disk_free(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"x" * 2048)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"y" * 1024)
    assert dir_size_bytes(tmp_path) == 3072
    assert disk_free_bytes(tmp_path) and disk_free_bytes(tmp_path) > 0
    assert disk_free_bytes(tmp_path / "does-not-exist") is None


def test_human_bytes_units() -> None:
    assert human_bytes(512) == "512.0 B"
    assert human_bytes(2048).endswith("KiB")
    assert human_bytes(5 * 1024**3).endswith("GiB")


# --------------------------------------------------------------------------- #
# process helpers
# --------------------------------------------------------------------------- #
def test_run_process_success_captures_output() -> None:
    result = run_process([sys.executable, "-c", "print('hello')"])
    assert result.ok is True
    assert "hello" in result.stdout
    assert result.duration_s >= 0
    assert result.to_dict()["returncode"] == 0


def test_run_process_failure_does_not_raise() -> None:
    result = run_process([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert result.ok is False
    assert result.returncode == 3
    assert result.error is None  # a non-zero exit is not an error object


def test_run_process_reports_missing_binary() -> None:
    result = run_process(["definitely-not-a-real-binary-xyz"])
    assert result.ok is False
    assert result.returncode != 0
    assert result.error and "not found" in result.error.lower()


def test_run_process_empty_command() -> None:
    result = run_process([])
    assert result.ok is False
    assert result.error == "empty command"


def test_run_process_times_out_cleanly() -> None:
    result = run_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.6)
    assert result.timed_out is True
    assert result.ok is False
    assert result.error and "timeout" in result.error.lower()


def test_run_process_truncates_huge_output() -> None:
    result = run_process([sys.executable, "-c", "print('x' * 6000)"], timeout=30)
    payload = result.to_dict()
    assert "chars truncated" in payload["stdout"]
    assert len(payload["stdout"]) < 4200


def test_procresult_properties() -> None:
    ok = ProcResult(cmd=["x"], returncode=0, stdout="a\nb", stderr="")
    assert ok.ok and ok.output == "a\nb" and ok.first_lines == "a\nb"
    noisy = ProcResult(cmd=["x"], returncode=1, stdout="1\n2\n3\n4\n5\n6\n7", stderr="err")
    assert noisy.ok is False
    assert noisy.first_lines.count("\n") == 5  # first six lines only
    assert "err" in noisy.output  # stderr is folded in


def test_wrap_command_is_a_noop_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    assert wrap_command(["git", "status"]) == ["git", "status"]


def test_wrap_command_handles_windows_launchers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    assert wrap_command(["tool.cmd", "--flag"])[0].endswith("cmd.exe")
    assert wrap_command(["tool.bat"])[1] == "/c"
    assert wrap_command(["script.ps1", "-x"])[:2] == ["powershell", "-NoProfile"]
    assert wrap_command(["native.exe"]) == ["native.exe"]


def test_resolve_executable_finds_real_and_fake_tools(tmp_path: Path) -> None:
    # the interpreter's own directory is passed explicitly, so this holds even when
    # PATH is minimal (CI containers) or the tests run from a stripped environment
    exe_dir = Path(sys.executable).parent
    assert resolve_executable(Path(sys.executable).name, extra_dirs=[exe_dir]) is not None
    assert resolve_executable("definitely-not-a-binary-xyz") is None

    fake_dir = tmp_path / "bin"
    fake_dir.mkdir()
    fake = fake_dir / "faketool"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    assert resolve_executable("faketool", extra_dirs=[fake_dir]) is not None


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
def test_platform_summary_shape() -> None:
    summary = platform_summary()
    for key in ("os", "machine", "python", "is_windows", "is_docker"):
        assert key in summary and isinstance(summary[key], str)


def test_unique_preserve_order_and_clamp() -> None:
    assert unique_preserve_order(["a", "b", "a", "c"]) == ["a", "b", "c"]
    assert clamp(5, 0, 10) == 5 and clamp(-1, 0, 10) == 0 and clamp(99, 0, 10) == 10


def test_degradation_defaults() -> None:
    note = Degradation(area="github_issues", detail="403 rate limited")
    assert note.area == "github_issues"
    assert note.fatal is False
    assert note.notes == []
