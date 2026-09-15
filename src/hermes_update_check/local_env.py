"""Local environment detection: what Hermes is installed here, how, and in what state.

Sources, in order of trust:

1. ``hermes --version`` (authoritative, includes install method + upstream sha);
2. ``git`` on the install directory (branch, commit, dirty tree, tags);
3. the filesystem (``$HERMES_HOME``, ``state.db``, venv layout);
4. a best-effort ``hermes update --check`` (informational, never the only source).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Config, resolve_hermes_home
from .errors import ConfigError
from .logging_setup import get_logger
from .util import ProcResult, resolve_executable, run_process
from .versioning import VersionOutput, normalise_tag, parse_version_output

DEFAULT_TIMEOUT = 30.0
UPDATE_CHECK_TIMEOUT = 90.0

VERSION_SOURCE_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


@dataclass
class GitState:
    """git facts about the install directory (all optional / best-effort)."""

    is_repo: bool = False
    branch: Optional[str] = None
    commit: Optional[str] = None
    full_commit: Optional[str] = None
    dirty: bool = False
    dirty_files: list[str] = field(default_factory=list)
    tags_at_head: list[str] = field(default_factory=list)
    describe: Optional[str] = None
    remote_url: Optional[str] = None
    upstream_ref: Optional[str] = None
    upstream_commit: Optional[str] = None
    last_commit_date: Optional[str] = None
    error: Optional[str] = None

    @property
    def on_main(self) -> bool:
        return (self.branch or "") in {"main", "master"}

    @property
    def at_release_tag(self) -> bool:
        return bool(self.tags_at_head)

    @property
    def clean(self) -> bool:
        return self.is_repo and not self.dirty

    def to_dict(self) -> dict[str, object]:
        return {
            "is_repo": self.is_repo,
            "branch": self.branch,
            "commit": self.commit,
            "full_commit": self.full_commit,
            "dirty": self.dirty,
            "dirty_files": self.dirty_files[:50],
            "tags_at_head": self.tags_at_head,
            "describe": self.describe,
            "remote_url": self.remote_url,
            "upstream_ref": self.upstream_ref,
            "upstream_commit": self.upstream_commit,
            "on_main": self.on_main,
            "error": self.error,
        }


@dataclass
class LocalEnv:
    """Everything we know about the local Hermes installation."""

    hermes_home: Path
    install_kind: str = "unknown"  # git | pip | uv | docker | nix | unknown
    install_dir: Optional[Path] = None
    hermes_cli: Optional[str] = None
    version: Optional[str] = None
    release_tag: Optional[str] = None
    commit: Optional[str] = None
    python_version: Optional[str] = None
    sdk_version: Optional[str] = None
    says_up_to_date: Optional[bool] = None
    git: Optional[GitState] = None
    venv_python: Optional[Path] = None
    uv_path: Optional[str] = None
    version_output: str = ""
    update_check_output: Optional[str] = None
    update_check_says_available: Optional[bool] = None
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- derived paths ------------------------------------------------------- #

    @property
    def config_path(self) -> Path:
        return self.hermes_home / "config.yaml"

    @property
    def env_file(self) -> Path:
        return self.hermes_home / ".env"

    @property
    def state_db(self) -> Path:
        return self.hermes_home / "state.db"

    @property
    def logs_dir(self) -> Path:
        return self.hermes_home / "logs"

    @property
    def sessions_dir(self) -> Path:
        return self.hermes_home / "sessions"

    @property
    def backups_dir(self) -> Path:
        return self.hermes_home / "backups"

    @property
    def is_git_install(self) -> bool:
        return self.install_kind == "git" and bool(self.git and self.git.is_repo)

    @property
    def tracking_main(self) -> bool:
        return bool(self.git and self.git.on_main)

    @property
    def version_label(self) -> str:
        if self.version and self.release_tag:
            return f"v{self.version} ({self.release_tag.lstrip('v')})"
        if self.version:
            return f"v{self.version}"
        if self.commit:
            return f"commit {self.commit}"
        return "unknown"

    def to_dict(self) -> dict[str, object]:
        return {
            "hermes_home": str(self.hermes_home),
            "config_path": str(self.config_path),
            "state_db": str(self.state_db),
            "install_kind": self.install_kind,
            "install_dir": str(self.install_dir) if self.install_dir else None,
            "hermes_cli": self.hermes_cli,
            "version": self.version,
            "release_tag": self.release_tag,
            "commit": self.commit,
            "python_version": self.python_version,
            "sdk_version": self.sdk_version,
            "says_up_to_date": self.says_up_to_date,
            "tracking_main": self.tracking_main,
            "git": self.git.to_dict() if self.git else None,
            "venv_python": str(self.venv_python) if self.venv_python else None,
            "uv_path": self.uv_path,
            "errors": self.errors,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #


def detect_local_env(
    cfg: Config,
    *,
    logger: Optional[logging.Logger] = None,
    run_upstream_check: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> LocalEnv:
    """Inspect the local Hermes installation. Never raises for missing pieces."""
    log = logger or get_logger("local_env")
    home = resolve_hermes_home(cfg)
    env = LocalEnv(hermes_home=home)

    if not home.exists():
        env.notes.append(f"HERMES_HOME does not exist yet: {home}")

    env.hermes_cli = resolve_executable("hermes", extra_dirs=[home / "bin"])
    if env.hermes_cli is None:
        env.errors.append("`hermes` executable not found on PATH (checked $HERMES_HOME/bin too)")

    version_output = _read_version_output(env.hermes_cli, timeout=timeout, log=log)
    if version_output is not None:
        env.version_output = version_output.raw
        env.version = version_output.version
        env.release_tag = normalise_tag(version_output.release_tag)
        env.commit = version_output.commit
        env.python_version = version_output.python_version
        env.sdk_version = version_output.sdk_version
        env.says_up_to_date = version_output.says_up_to_date
        if version_output.install_dir:
            candidate = Path(version_output.install_dir)
            if candidate.exists():
                env.install_dir = candidate
            else:
                env.notes.append(f"reported install dir does not exist: {candidate}")
        if version_output.install_method:
            env.install_kind = _normalise_install_kind(version_output.install_method)
        env.notes.extend(version_output.notes)

    install_dir = env.install_dir or (home / "hermes-agent")
    if install_dir.exists():
        env.install_dir = install_dir
    else:
        env.install_dir = env.install_dir if env.install_dir and env.install_dir.exists() else None

    env.git = detect_git_state(env.install_dir, log=log)
    if env.git.is_repo:
        env.install_kind = "git" if env.install_kind in {"git", "unknown"} else env.install_kind
        if env.commit is None:
            env.commit = env.git.commit
    elif env.install_kind == "unknown":
        env.install_kind = _infer_install_kind(home, log=log)

    if env.version is None and env.install_dir:
        fallback = _version_from_source(env.install_dir)
        if fallback:
            env.version = fallback
            env.notes.append("version read from hermes_cli/__init__.py (hermes --version unavailable)")

    env.venv_python = _find_venv_python(home, env.install_dir)
    env.uv_path = resolve_executable("uv", extra_dirs=[home / "bin", home / "uv"])

    if run_upstream_check and env.hermes_cli:
        env.update_check_output, env.update_check_says_available = _run_update_check(env.hermes_cli, timeout=UPDATE_CHECK_TIMEOUT, log=log)

    return env


def _read_version_output(hermes_cli: Optional[str], *, timeout: float, log: logging.Logger) -> Optional[VersionOutput]:
    if not hermes_cli:
        return None
    result = run_process([hermes_cli, "--version"], timeout=timeout)
    if not result.ok and not result.output:
        log.debug("hermes --version failed: %s", result.error or result.returncode)
        return None
    parsed = parse_version_output(result.output)
    if not parsed.parsed:
        # Some wrappers print a banner first; retry with the venv python directly.
        log.debug("version output not recognised: %r", result.output[:200])
    return parsed


def _run_update_check(hermes_cli: str, *, timeout: float, log: logging.Logger) -> tuple[Optional[str], Optional[bool]]:
    """`hermes update --check` - used as a *signal*, never as the only source."""
    result = run_process([hermes_cli, "update", "--check"], timeout=timeout)
    output = result.output.strip() or None
    if output is None:
        log.debug("hermes update --check produced no output (%s)", result.error)
        return None, None
    lowered = output.lower()
    available: Optional[bool] = None
    if re.search(r"\bupdate available\b|\bnew version\b|\bnewer\b", lowered):
        available = True
    elif re.search(r"\bup to date\b|\balready up[- ]to[- ]date\b|\bno updates?\b", lowered):
        available = False
    return output, available


def detect_git_state(install_dir: Optional[Path], *, log: Optional[logging.Logger] = None) -> GitState:
    """Collect git facts (no network: never fetches)."""
    logger = log or get_logger("git")
    state = GitState()
    if install_dir is None:
        state.error = "install directory unknown"
        return state
    git_dir = install_dir / ".git"
    if not git_dir.exists():
        state.error = f"no .git directory in {install_dir}"
        return state
    state.is_repo = True

    status = _git(install_dir, ["status", "--porcelain"], logger)
    if status is not None:
        lines = [line for line in status.splitlines() if line.strip()]
        state.dirty = bool(lines)
        state.dirty_files = [line.strip() for line in lines]

    branch = _git(install_dir, ["rev-parse", "--abbrev-ref", "HEAD"], logger)
    if branch:
        state.branch = branch.strip()

    full = _git(install_dir, ["rev-parse", "HEAD"], logger)
    if full:
        state.full_commit = full.strip()
        state.commit = full.strip()[:8]

    describe = _git(install_dir, ["describe", "--tags", "--always"], logger)
    if describe:
        state.describe = describe.strip()

    tags = _git(install_dir, ["tag", "--points-at", "HEAD"], logger)
    if tags:
        state.tags_at_head = [t.strip() for t in tags.splitlines() if t.strip()]

    remote = _git(install_dir, ["remote", "get-url", "origin"], logger)
    if remote:
        state.remote_url = remote.strip()

    if state.branch:
        upstream = _git(install_dir, ["rev-parse", "--verify", f"origin/{state.branch}"], logger)
        if upstream and upstream.strip():
            state.upstream_ref = f"origin/{state.branch}"
            state.upstream_commit = upstream.strip()[:8]

    date = _git(install_dir, ["log", "-1", "--format=%cI"], logger)
    if date:
        state.last_commit_date = date.strip()
    return state


def _git(cwd: Path, args: list[str], log: logging.Logger) -> Optional[str]:
    result = run_process(["git", "-C", str(cwd), *args], timeout=20.0)
    if result.ok:
        return result.stdout
    log.debug("git %s failed: %s", " ".join(args), result.error or result.stderr.strip()[:200])
    return None


def _normalise_install_kind(raw: str) -> str:
    text = raw.strip().lower()
    for kind in ("git", "docker", "nix", "pip", "uv"):
        if kind in text:
            return kind
    return "unknown"


def _infer_install_kind(home: Path, *, log: logging.Logger) -> str:
    """Fallback detection when `hermes --version` did not tell us."""
    if Path("/.dockerenv").exists():
        return "docker"
    if (home / "hermes-agent" / ".git").exists():
        return "git"
    for candidate in (home / "hermes-agent" / "venv", home / "venv"):
        for python in (candidate / "bin" / "python", candidate / "Scripts" / "python.exe"):
            if python.exists():
                probe = run_process([str(python), "-m", "pip", "show", "hermes-agent"], timeout=25.0)
                if probe.ok and "Name: hermes-agent" in probe.stdout:
                    return "pip"
    if (home / "flake.nix").exists():
        return "nix"
    log.debug("could not infer install kind under %s", home)
    return "unknown"


def _version_from_source(install_dir: Path) -> Optional[str]:
    """Read ``__version__`` straight from the source tree (no interpreter needed)."""
    candidate = install_dir / "hermes_cli" / "__init__.py"
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = VERSION_SOURCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return None


def _find_venv_python(home: Path, install_dir: Optional[Path]) -> Optional[Path]:
    roots = [install_dir, home / "hermes-agent", home] if install_dir else [home / "hermes-agent", home]
    for root in roots:
        if root is None:
            continue
        for rel in (Path("venv") / "bin" / "python", Path("venv") / "Scripts" / "python.exe", Path(".venv") / "bin" / "python", Path(".venv") / "Scripts" / "python.exe"):
            candidate = root / rel
            if candidate.exists():
                return candidate
    return None


# --------------------------------------------------------------------------- #
# process / service helpers (used by preflight and health checks)
# --------------------------------------------------------------------------- #


@dataclass
class ProcessInfo:
    pid: int
    args: str
    elapsed: str = ""

    @property
    def short(self) -> str:
        text = self.args.strip()
        return text if len(text) <= 110 else text[:107] + "..."


def list_hermes_processes(*, timeout: float = 20.0) -> tuple[list[ProcessInfo], Optional[str]]:
    """Running processes that look like Hermes (best-effort, never fatal).

    Returns ``(processes, error)``. The current process tree is excluded when
    it is obviously *this* tool (otherwise every check would warn about itself).
    """
    if os.name == "nt":
        return _list_windows_processes(timeout=timeout)
    return _list_posix_processes(timeout=timeout)


def _list_posix_processes(*, timeout: float) -> tuple[list[ProcessInfo], Optional[str]]:
    result = run_process(["ps", "-eo", "pid,etime,args"], timeout=timeout)
    if not result.ok:
        return [], result.error or "ps failed"
    processes: list[ProcessInfo] = []
    own_pid = os.getpid()
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_text, elapsed, args = parts
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == own_pid:
            continue
        lowered = args.lower()
        if "hermes" not in lowered:
            continue
        if "hermes-update-check" in lowered:
            continue
        processes.append(ProcessInfo(pid=pid, args=args, elapsed=elapsed))
    return processes, None


def _list_windows_processes(*, timeout: float) -> tuple[list[ProcessInfo], Optional[str]]:
    """Find Hermes processes on Windows.

    ``tasklist`` only reports process *names*, which makes "python.exe" and
    "node.exe" useless as a filter - so ask PowerShell/CIM for the real command
    line first and only fall back to name matching.
    """
    processes = _list_windows_processes_via_cim(timeout=timeout)
    if processes is not None:
        return processes, None
    return _list_windows_processes_via_tasklist(timeout=timeout)


def _list_windows_processes_via_cim(*, timeout: float) -> Optional[list[ProcessInfo]]:
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match 'hermes' } | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
    )
    result = run_process(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=timeout)
    if not result.ok:
        return None
    out: list[ProcessInfo] = []
    own_pid = os.getpid()
    for line in result.stdout.splitlines():
        pid_text, _, command = line.strip().partition("\t")
        if not command.strip():
            continue
        try:
            pid = int(pid_text.strip())
        except ValueError:
            continue
        if pid == own_pid or "hermes-update-check" in command.lower():
            continue
        out.append(ProcessInfo(pid=pid, args=command.strip()))
    return out


def _list_windows_processes_via_tasklist(*, timeout: float) -> tuple[list[ProcessInfo], Optional[str]]:
    result = run_process(["tasklist", "/FO", "CSV", "/NH"], timeout=timeout)
    if not result.ok:
        return [], result.error or "tasklist failed"
    processes: list[ProcessInfo] = []
    for line in result.stdout.splitlines():
        cells = [c.strip('"') for c in line.split('","')]
        if len(cells) < 2:
            continue
        name = cells[0].strip('"')
        lowered = name.lower()
        # Name-only matching: restrict to names that actually contain "hermes".
        if "hermes" not in lowered:
            continue
        try:
            pid = int(cells[1])
        except ValueError:
            continue
        processes.append(ProcessInfo(pid=pid, args=name, elapsed=("(command line unavailable)")))
    return processes, None


def gateway_status(hermes_cli: Optional[str], *, timeout: float = 45.0) -> ProcResult:
    """Run ``hermes gateway status`` (empty ProcResult when the CLI is missing)."""
    if not hermes_cli:
        return ProcResult(cmd=[], returncode=-1, error="hermes executable not found")
    return run_process([hermes_cli, "gateway", "status"], timeout=timeout)


def parse_gateway_status(result: ProcResult) -> tuple[str, str]:
    """Classify gateway status output into PASS / WARN / FAIL + a short detail."""
    if result.error:
        return "WARN", f"could not query gateway status: {result.error}"
    text = result.output.lower()
    if not text.strip():
        return "WARN", "gateway status returned no output"
    if "not running" in text or "stopped" in text or "inactive" in text:
        return "WARN", "gateway is not running"
    if "running" in text or "active" in text or "healthy" in text or result.returncode == 0:
        return "PASS", "gateway is running"
    return "WARN", "gateway status unclear"


def require_hermes_home(cfg: Config) -> Path:
    """Hard requirement for commands that touch the installation."""
    home = resolve_hermes_home(cfg)
    if not home.exists():
        raise ConfigError(
            f"HERMES_HOME not found: {home}",
            hint="set HERMES_HOME or paths.hermes_home in the config file",
        )
    return home
