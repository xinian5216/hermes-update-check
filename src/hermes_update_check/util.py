"""Small, dependency-free helpers: subprocess runner, JSON IO, time parsing, hashing.

Everything here must work on Linux (Debian/Ubuntu first), macOS and Windows,
and must never raise on "expected" failures - callers get a structured result
and decide what to do.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

# --------------------------------------------------------------------------- #
# time
# --------------------------------------------------------------------------- #


def utcnow() -> datetime:
    """Timezone-aware UTC now (naive datetimes are a bug factory)."""
    return datetime.now(timezone.utc)


def parse_iso8601(value: str | None) -> datetime | None:
    """Parse the ISO-8601 timestamps the GitHub API returns (``...Z`` included)."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Python 3.9/3.10 cannot parse fractional seconds with >6 digits
        try:
            head, _, tail = text.partition(".")
            frac, _, tz = tail.partition("+")
            text = f"{head}.{frac[:6]}+{tz}" if tz else f"{head}.{frac[:6]}"
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def hours_between(older: datetime, newer: datetime | None = None) -> float:
    """Hours from ``older`` to ``newer`` (default: now). Negative if in the future."""
    newer = newer or utcnow()
    return (newer - older).total_seconds() / 3600.0


def humanize_hours(hours: float, *, lang: str = "zh") -> str:
    """'3.2 days', '5.4 hours', '2.1 months' style age string."""
    if hours < 0:
        hours = 0.0
    if hours < 1:
        return ("不足 1 小时" if lang == "zh" else "less than an hour")
    if hours < 48:
        return (f"{hours:.1f} 小时" if lang == "zh" else f"{hours:.1f} hours")
    days = hours / 24.0
    if days < 60:
        return (f"{days:.1f} 天" if lang == "zh" else f"{days:.1f} days")
    return (f"{days / 30.0:.1f} 个月" if lang == "zh" else f"{days / 30.0:.1f} months")


def iso(dt: datetime | None) -> str | None:
    """Serialise a datetime in UTC ISO-8601 with a trailing Z."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_local(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def later_of(*dts: datetime | None) -> datetime | None:
    real = [d for d in dts if d is not None]
    return max(real) if real else None


def days_ago(days: float, *, now: datetime | None = None) -> datetime:
    return (now or utcnow()) - timedelta(days=days)


# --------------------------------------------------------------------------- #
# filesystem / json
# --------------------------------------------------------------------------- #


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> Any | None:
    """Read JSON; return None when the file is missing or corrupt."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: Any) -> Path:
    """Atomically write JSON (tmp file + os.replace) so a crashed run cannot corrupt state."""
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        with suppress_oserror():
            tmp_path.unlink()
        raise
    return path


def write_text(path: Path, text: str) -> Path:
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        with suppress_oserror():
            tmp_path.unlink()
        raise
    return path


class suppress_oserror:  # noqa: N801 - context manager reads better lowercase
    """``contextlib.suppress(OSError)`` without the import ceremony."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001 - protocol signature
        return exc_type is not None and issubclass(exc_type, OSError)


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str | None:
    """Hash a file (None when unreadable); used to record config fingerprints."""
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def dir_size_bytes(path: Path, *, limit_files: int = 200_000) -> int:
    """Best-effort recursive size; never raises."""
    total = 0
    seen = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
                seen += 1
                if seen >= limit_files:
                    return total
    except OSError:
        pass
    return total


def human_bytes(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PiB"


def disk_free_bytes(path: Path | str) -> int | None:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# subprocess
# --------------------------------------------------------------------------- #


@dataclass
class ProcResult:
    """Outcome of an external command. Never throws for non-zero exit codes."""

    cmd: list[str]
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.error is None and not self.timed_out

    @property
    def output(self) -> str:
        """stdout+stderr combined - some CLIs print to stderr."""
        parts = [p for p in (self.stdout.strip(), self.stderr.strip()) if p]
        return "\n".join(parts)

    @property
    def first_lines(self) -> str:
        return "\n".join(self.output.splitlines()[:6])

    def to_dict(self) -> dict[str, Any]:
        return {
            "cmd": self.cmd,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "error": self.error,
            "duration_s": round(self.duration_s, 3),
            "stdout": _truncate(self.stdout, 4000),
            "stderr": _truncate(self.stderr, 4000),
        }


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} chars truncated]"


def resolve_executable(name: str | os.PathLike[str], *, extra_dirs: Sequence[Path] = ()) -> str | None:
    """Find an executable cross-platform.

    ``shutil.which`` on Windows honours PATHEXT (``hermes.cmd`` etc.). Extra
    directories are checked first so a launcher shipped inside ``$HERMES_HOME/bin``
    wins over a stale PATH entry.
    """
    raw = str(name)
    looks_like_path = os.sep in raw or "/" in raw or "\\" in raw
    if looks_like_path:
        p = Path(raw)
        return str(p) if p.exists() else None

    for directory in extra_dirs:
        for candidate in _name_variants(raw):
            found = directory / candidate
            if found.exists():
                return str(found)
    return shutil.which(raw)


def _name_variants(name: str) -> list[str]:
    if os.name != "nt":
        return [name]
    return [name, f"{name}.cmd", f"{name}.bat", f"{name}.exe", f"{name}.ps1"]


def run_process(
    cmd: Sequence[str],
    *,
    timeout: float = 60.0,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stdin_text: str | None = None,
) -> ProcResult:
    """Run a command with a hard timeout; capture output; never raise.

    Windows note: ``.cmd``/``.bat`` launchers cannot be executed by
    CreateProcess directly, so they are wrapped in ``cmd.exe /c``.
    """
    argv = [str(part) for part in cmd]
    if not argv:
        return ProcResult(cmd=[], returncode=-1, error="empty command")

    argv = _windows_wrap(argv)

    proc_env = None
    if env is not None:
        proc_env = dict(os.environ)
        proc_env.update({str(k): str(v) for k, v in env.items()})

    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env=proc_env,
            input=stdin_text,
        )
    except subprocess.TimeoutExpired as exc:
        return ProcResult(
            cmd=argv,
            returncode=-1,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
            timed_out=True,
            error=f"timeout after {timeout:g}s",
            duration_s=time.monotonic() - started,
        )
    except FileNotFoundError as exc:
        return ProcResult(
            cmd=argv,
            returncode=-1,
            error=f"executable not found: {exc.filename or argv[0]}",
            duration_s=time.monotonic() - started,
        )
    except OSError as exc:
        return ProcResult(
            cmd=argv,
            returncode=-1,
            error=f"OSError: {exc}",
            duration_s=time.monotonic() - started,
        )
    return ProcResult(
        cmd=argv,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_s=time.monotonic() - started,
    )


def _windows_wrap(argv: list[str]) -> list[str]:
    if os.name != "nt":
        return argv
    first = argv[0]
    lower = first.lower()
    if lower.endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/c", first, *argv[1:]]
    if lower.endswith(".ps1"):
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", first, *argv[1:]]
    return argv


def wrap_command(argv: Sequence[str]) -> list[str]:
    """Public wrapper: make an argv list executable on this platform (Windows .cmd/.bat/.ps1)."""
    return _windows_wrap([str(part) for part in argv])


def run_streaming(
    cmd: Sequence[str],
    *,
    timeout: float = 1800.0,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    on_line: Optional["Callable[[str], None]"] = None,
    max_buffer_chars: int = 400_000,
) -> ProcResult:
    """Run a long command, streaming its output line by line to ``on_line``.

    Used for ``hermes update``: an update can take minutes and the user deserves
    to see what it is doing instead of a frozen prompt. Output is buffered
    (capped) so it is still available for the report and the log.
    """
    argv = wrap_command(cmd)
    if not argv:
        return ProcResult(cmd=[], returncode=-1, error="empty command")

    proc_env = None
    if env is not None:
        proc_env = dict(os.environ)
        proc_env.update({str(k): str(v) for k, v in env.items()})

    started = time.monotonic()
    lines: list[str] = []
    buffered = 0
    try:
        process = subprocess.Popen(  # noqa: S603 - argv list, no shell
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(cwd) if cwd else None,
            env=proc_env,
        )
    except FileNotFoundError as exc:
        return ProcResult(cmd=argv, returncode=-1, error=f"executable not found: {exc.filename or argv[0]}")
    except OSError as exc:
        return ProcResult(cmd=argv, returncode=-1, error=f"OSError: {exc}")

    timed_out = False
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:  # a broken renderer must not kill the update
                    pass
            if buffered < max_buffer_chars:
                lines.append(line)
                buffered += len(line)
            if time.monotonic() - started > timeout:
                timed_out = True
                _terminate(process)
                break
        returncode = process.wait(timeout=30)
    except Exception as exc:  # pragma: no cover - defensive
        _terminate(process)
        return ProcResult(
            cmd=argv,
            returncode=-1,
            stdout="\n".join(lines),
            error=f"{type(exc).__name__}: {exc}",
            duration_s=time.monotonic() - started,
        )

    return ProcResult(
        cmd=argv,
        returncode=returncode,
        stdout="\n".join(lines),
        timed_out=timed_out,
        error=f"timeout after {timeout:g}s (process killed)" if timed_out else None,
        duration_s=time.monotonic() - started,
    )


def _terminate(process: "subprocess.Popen[str]") -> None:
    try:
        process.terminate()
        process.wait(timeout=15)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #


def platform_summary() -> dict[str, str]:
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "is_windows": str(os.name == "nt"),
        "is_docker": str(Path("/.dockerenv").exists()),
    }


def unique_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class Degradation:
    """A note about data we could not obtain (rendered as UNKNOWN, never as 'safe')."""

    area: str
    detail: str
    fatal: bool = False
    notes: list[str] = field(default_factory=list)
