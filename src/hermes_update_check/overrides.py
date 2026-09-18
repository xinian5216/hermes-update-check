"""Managed Local Overrides: intentional local customization is not corruption.

Users customize their Hermes checkout on purpose (remove a UI element, change a
default, patch a prompt). Git calls that "dirty" and the phase-3 model blocked
every update because of it - so a user who customizes can never be told anything
useful. This module separates the two very different things git status mixes:

    known dirty    - registered, hashed, patchable, restorable  -> manageable
    unknown dirty  - leftovers, half-finished work, surprises   -> still blocks

Design rules (do not break them):

* nothing is registered automatically - ``detect`` only reports and asks;
* a registry entry stores the *real* baseline (base commit + base content hash
  + the generated patch), never just a filename;
* patches (``git diff --binary``) plus metadata are the durable format -
  ``git stash`` may be used for a transient step but never as the record;
* unknown changes always block an update; drift is never assumed safe;
* user modifications are never destroyed to make an update succeed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .util import ProcResult, ensure_dir, iso, read_json, run_process, utcnow, write_json

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

OVERRIDES_DIRNAME = "overrides"
REGISTRY_NAME = "registry.json"
PATCHES_DIRNAME = "patches"
SNAPSHOTS_DIRNAME = "snapshots"
REGISTRY_VERSION = 1

POLICY_PRESERVE = "preserve"

STATUS_MODIFIED = "modified"
STATUS_ADDED = "added"
STATUS_DELETED = "deleted"
STATUS_RENAMED = "renamed"
STATUS_UNTRACKED = "untracked"
STATUS_IGNORED = "ignored"
STATUS_UNKNOWN = "unknown"

# how a registered override compares with what is on disk right now
MATCH_EXACT = "exact"  # unchanged since registration
MATCH_DRIFTED = "drifted"  # still customized, but edited again since registration
MATCH_MISSING = "missing"  # registered, but the local change is gone
MATCH_UNKNOWN = "unknown"  # cannot tell (no git, unreadable file)

SAFETY_PASS = "PASS"
SAFETY_WARN = "WARN"
SAFETY_FAIL = "FAIL"
SAFETY_UNKNOWN = "UNKNOWN"

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"
CONFIDENCE_UNKNOWN = "UNKNOWN"

# registerable statuses: a *local change* was made to a tracked file
REGISTERABLE_STATUSES = {STATUS_MODIFIED, STATUS_ADDED, STATUS_DELETED, STATUS_RENAMED}

_HUNK_RE = re.compile(r"^@@ -(?P<start>\d+)(?:,(?P<len>\d+))? \+\d+(?:,\d+)? @@")
_DIFF_PATH_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$")


class OverrideError(Exception):
    """Raised for refused operations (never for a mere detection problem)."""


# --------------------------------------------------------------------------- #
# running git
# --------------------------------------------------------------------------- #

RunFn = Callable[..., ProcResult]


def _run(
    install_dir: Path, args: Sequence[str], *, runner: Optional[RunFn] = None, timeout: float = 120.0
) -> ProcResult:
    """Run one git command inside the install directory; never raises."""
    cmd = ["git", "-c", "core.quotepath=false", *args]
    run = runner or run_process
    try:
        return run(cmd, cwd=install_dir, timeout=timeout)
    except TypeError:  # a stub without cwd/timeout
        return run(cmd)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("git %s failed: %s", " ".join(args), exc)
        return ProcResult(returncode=127, stdout="", stderr=str(exc))


def run_git(
    install_dir: Path, args: Sequence[str], *, runner: Optional[RunFn] = None, timeout: float = 120.0
) -> ProcResult:
    """Public wrapper so other modules share one git invocation path."""
    return _run(install_dir, args, runner=runner, timeout=timeout)


def _run_bytes(install_dir: Path, args: Sequence[str], *, timeout: float = 120.0) -> bytes:
    """Run git with binary capture - text mode would mangle binary blobs."""
    cmd = ["git", "-c", "core.quotepath=false", *args]
    try:
        proc = subprocess.run(cmd, cwd=str(install_dir), capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - defensive
        logger.debug("git %s failed: %s", " ".join(args), exc)
        return b""
    return proc.stdout if proc.returncode == 0 else b""


def git_available(*, runner: Optional[RunFn] = None, install_dir: Optional[Path] = None) -> bool:
    """Probe git by *running* it - `command -v` lies on Windows."""
    cwd = install_dir or Path.cwd()
    result = _run(cwd, ["--version"], runner=runner, timeout=20.0)
    return result.returncode == 0 and "git" in (result.stdout or "").lower()


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #


@dataclass
class LocalChange:
    """One line of ``git status --porcelain``."""

    path: str
    status: str
    index_code: str = " "
    worktree_code: str = " "
    untracked: bool = False
    ignored: bool = False
    renamed_from: Optional[str] = None

    @property
    def registerable(self) -> bool:
        return self.status in REGISTERABLE_STATUSES and not self.untracked and not self.ignored

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "untracked": self.untracked,
            "ignored": self.ignored,
            "renamed_from": self.renamed_from,
        }


@dataclass
class OverrideEntry:
    """One registered local customization."""

    path: str
    status: str = STATUS_MODIFIED
    policy: str = POLICY_PRESERVE
    base_commit: str = ""
    base_sha256: str = ""
    current_sha256: str = ""
    patch_sha256: str = ""
    patch_file: str = ""
    snapshot_file: str = ""
    registered_at: str = ""
    match: str = MATCH_UNKNOWN
    notes_zh: str = ""
    notes_en: str = ""

    @property
    def exact(self) -> bool:
        return self.match == MATCH_EXACT

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "policy": self.policy,
            "base_commit": self.base_commit,
            "base_sha256": self.base_sha256,
            "current_sha256": self.current_sha256,
            "patch_sha256": self.patch_sha256,
            "patch_file": self.patch_file,
            "snapshot_file": self.snapshot_file,
            "registered_at": self.registered_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> OverrideEntry:
        return cls(
            path=str(raw.get("path", "")),
            status=str(raw.get("status", STATUS_MODIFIED)),
            policy=str(raw.get("policy", POLICY_PRESERVE)),
            base_commit=str(raw.get("base_commit", "")),
            base_sha256=str(raw.get("base_sha256", "")),
            current_sha256=str(raw.get("current_sha256", "")),
            patch_sha256=str(raw.get("patch_sha256", "")),
            patch_file=str(raw.get("patch_file", "")),
            snapshot_file=str(raw.get("snapshot_file", "")),
            registered_at=str(raw.get("registered_at", "")),
        )


@dataclass
class OverrideRegistry:
    """The local override registry (``~/.hermes-update-check/overrides/registry.json``)."""

    version: int = REGISTRY_VERSION
    base_commit: str = ""
    base_branch: str = ""
    base_release: str = ""
    origin_main: str = ""
    registered_at: str = ""
    files: list[OverrideEntry] = field(default_factory=list)
    broken: bool = False
    broken_reason: str = ""

    @property
    def empty(self) -> bool:
        return not self.files

    def paths(self) -> list[str]:
        return [entry.path for entry in self.files]

    def entry(self, path: str) -> Optional[OverrideEntry]:
        for entry in self.files:
            if entry.path == path:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "base_commit": self.base_commit,
            "base_branch": self.base_branch,
            "base_release": self.base_release,
            "origin_main": self.origin_main,
            "registered_at": self.registered_at,
            "files": [entry.to_dict() for entry in self.files],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> OverrideRegistry:
        files = [
            OverrideEntry.from_dict(item)
            for item in raw.get("files", [])
            if isinstance(item, Mapping) and str(item.get("path", "")).strip()
        ]
        return cls(
            version=int(raw.get("version", REGISTRY_VERSION) or REGISTRY_VERSION),
            base_commit=str(raw.get("base_commit", "")),
            base_branch=str(raw.get("base_branch", "")),
            base_release=str(raw.get("base_release", "")),
            origin_main=str(raw.get("origin_main", "")),
            registered_at=str(raw.get("registered_at", "")),
            files=files,
        )


@dataclass
class ReapplyPrediction:
    """How likely a registered override survives the trip to ``target``."""

    confidence: str = CONFIDENCE_UNKNOWN
    target: str = ""
    per_file: dict[str, str] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    upstream_touched: list[str] = field(default_factory=list)
    reasons_zh: list[str] = field(default_factory=list)
    reasons_en: list[str] = field(default_factory=list)

    @property
    def manual_merge_likely(self) -> bool:
        return self.confidence == CONFIDENCE_LOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "target": self.target,
            "per_file": dict(self.per_file),
            "conflicts": list(self.conflicts),
            "upstream_touched": list(self.upstream_touched),
            "manual_merge_likely": self.manual_merge_likely,
        }


@dataclass
class OverrideReport:
    """Classification of the working tree against the registry."""

    managed: list[LocalChange] = field(default_factory=list)
    managed_exact: list[str] = field(default_factory=list)
    drifted: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unknown: list[LocalChange] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    safety: str = SAFETY_UNKNOWN
    registry: Optional[OverrideRegistry] = None
    prediction: Optional[ReapplyPrediction] = None
    reasons_zh: list[str] = field(default_factory=list)
    reasons_en: list[str] = field(default_factory=list)

    @property
    def managed_count(self) -> int:
        return len(self.managed)

    @property
    def unknown_count(self) -> int:
        return len(self.unknown)

    @property
    def drifted_count(self) -> int:
        return len(self.drifted)

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    @property
    def clean(self) -> bool:
        return not self.managed and not self.unknown and not self.drifted and not self.missing

    @property
    def blocks_update(self) -> bool:
        """Unknown changes always block; drift is a block unless the user relaxed it."""
        return bool(self.unknown) or bool(self.drifted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "managed": [change.to_dict() for change in self.managed],
            "managed_count": self.managed_count,
            "managed_exact": list(self.managed_exact),
            "drifted": list(self.drifted),
            "missing": list(self.missing),
            "unknown": [change.to_dict() for change in self.unknown],
            "unknown_count": self.unknown_count,
            "ignored": list(self.ignored),
            "safety": self.safety,
            "registry_empty": bool(self.registry and self.registry.empty),
            "registry_broken": bool(self.registry and self.registry.broken),
            "prediction": self.prediction.to_dict() if self.prediction else None,
            "reasons_zh": list(self.reasons_zh),
            "reasons_en": list(self.reasons_en),
        }


@dataclass
class RegisterResult:
    registered: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    patch_file: str = ""
    reasons_zh: list[str] = field(default_factory=list)
    reasons_en: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "registered": list(self.registered),
            "refreshed": list(self.refreshed),
            "skipped": list(self.skipped),
            "refused": list(self.refused),
            "patch_file": self.patch_file,
            "reasons_zh": list(self.reasons_zh),
            "reasons_en": list(self.reasons_en),
        }


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    strategy: str = ""
    dry_run: bool = False
    output: str = ""
    conflicts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": list(self.applied),
            "failed": list(self.failed),
            "strategy": self.strategy,
            "dry_run": self.dry_run,
            "conflicts": list(self.conflicts),
            "ok": self.ok,
        }


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #


def overrides_root(state_root: Path) -> Path:
    return Path(state_root) / OVERRIDES_DIRNAME


def patches_dir(state_root: Path) -> Path:
    return overrides_root(state_root) / PATCHES_DIRNAME


def snapshots_dir(state_root: Path) -> Path:
    return overrides_root(state_root) / SNAPSHOTS_DIRNAME


def registry_path(state_root: Path) -> Path:
    return overrides_root(state_root) / REGISTRY_NAME


def load_registry(state_root: Path) -> OverrideRegistry:
    """Read the registry; a corrupt file yields a *broken* registry, never a crash."""
    path = registry_path(state_root)
    if not path.is_file():
        return OverrideRegistry()
    raw = read_json(path)
    if not isinstance(raw, Mapping):
        logger.warning("override registry is not a JSON object: %s", path)
        return OverrideRegistry(broken=True, broken_reason="registry.json is not a JSON object")
    try:
        registry = OverrideRegistry.from_dict(raw)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("override registry could not be parsed: %s", exc)
        return OverrideRegistry(broken=True, broken_reason=f"registry.json is unreadable: {exc}")
    if int(registry.version or 0) > REGISTRY_VERSION:
        registry.broken = True
        registry.broken_reason = f"registry version {registry.version} is newer than this tool understands"
    return registry


def save_registry(state_root: Path, registry: OverrideRegistry) -> Path:
    ensure_dir(overrides_root(state_root))
    return write_json(registry_path(state_root), registry.to_dict())


# --------------------------------------------------------------------------- #
# hashing / snapshots / patches
# --------------------------------------------------------------------------- #


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_of_file(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def working_tree_sha256(install_dir: Path, path: str) -> str:
    """sha256 of the file as it is on disk ("" when it does not exist)."""
    digest = _sha256_of_file(Path(install_dir) / path)
    return digest or ""


def blob_sha256(install_dir: Path, path: str, rev: str = "HEAD", *, runner: Optional[RunFn] = None) -> str:
    """sha256 of the file *content* at ``rev`` ("" when it does not exist there)."""
    data = _run_bytes(install_dir, ["show", f"{rev}:{path}"])
    return sha256_bytes(data) if data else ""


def _sanitise(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip("/").replace("\\", "/"))
    return safe[-120:] or "entry"


def snapshot_path(state_root: Path, stamp: str, path: str) -> Path:
    return snapshots_dir(state_root) / stamp / _sanitise(path)


def write_patch(
    state_root: Path, stamp: str, install_dir: Path, paths: Sequence[str], *, runner: Optional[RunFn] = None
) -> tuple[str, Path]:
    """``git diff --binary`` for the tracked paths; returns (sha256, file)."""
    args = ["diff", "--binary", "HEAD", "--", *paths]
    result = _run(install_dir, args, runner=runner, timeout=180.0)
    text = result.stdout or ""
    if result.returncode != 0:
        raise OverrideError(f"git diff failed: {(result.stderr or '').strip()[:200]}")
    target = patches_dir(state_root) / f"{stamp}.patch"
    ensure_dir(target.parent)
    target.write_bytes(text.encode("utf-8"))
    return sha256_bytes(text.encode("utf-8")), target


def read_patch(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# git status parsing
# --------------------------------------------------------------------------- #


def parse_porcelain(text: str) -> list[LocalChange]:
    """Parse ``git status --porcelain -uall --ignored=matching`` output."""
    changes: list[LocalChange] = []
    for raw_line in (text or "").splitlines():
        if not raw_line.strip():
            continue
        index_code = raw_line[0] if len(raw_line) > 0 else " "
        worktree_code = raw_line[1] if len(raw_line) > 1 else " "
        rest = raw_line[3:] if len(raw_line) > 3 else ""
        renamed_from = None
        if " -> " in rest and index_code in {"R", "C"}:
            renamed_from, rest = rest.split(" -> ", 1)
            renamed_from = renamed_from.strip().strip('"')
        path = rest.strip().strip('"')
        if index_code == "!" or worktree_code == "!":
            changes.append(
                LocalChange(
                    path=path, status=STATUS_IGNORED, index_code=index_code, worktree_code=worktree_code, ignored=True
                )
            )
            continue
        if index_code == "?" or worktree_code == "?":
            changes.append(
                LocalChange(
                    path=path,
                    status=STATUS_UNTRACKED,
                    index_code=index_code,
                    worktree_code=worktree_code,
                    untracked=True,
                )
            )
            continue
        code = index_code if index_code not in {" ", "?"} else worktree_code
        status = {
            "M": STATUS_MODIFIED,
            "A": STATUS_ADDED,
            "D": STATUS_DELETED,
            "R": STATUS_RENAMED,
            "C": STATUS_RENAMED,
            "T": STATUS_MODIFIED,
            "U": STATUS_MODIFIED,
        }.get(code, STATUS_UNKNOWN)
        changes.append(
            LocalChange(
                path=path,
                status=status,
                index_code=index_code,
                worktree_code=worktree_code,
                renamed_from=renamed_from,
            )
        )
    return changes


def detect_changes(install_dir: Path, *, runner: Optional[RunFn] = None) -> list[LocalChange]:
    """Every local change git can see (tracked, untracked, ignored)."""
    result = _run(install_dir, ["status", "--porcelain", "-uall", "--ignored=matching"], runner=runner, timeout=120.0)
    if result.returncode != 0:
        logger.debug("git status failed: %s", (result.stderr or "").strip()[:200])
        return []
    return parse_porcelain(result.stdout or "")


def split_changes(changes: Iterable[LocalChange]) -> tuple[list[LocalChange], list[LocalChange], list[str]]:
    """(tracked modifications, untracked files, ignored files)."""
    tracked: list[LocalChange] = []
    untracked: list[LocalChange] = []
    ignored: list[str] = []
    for change in changes:
        if change.ignored:
            ignored.append(change.path)
        elif change.untracked:
            untracked.append(change)
        else:
            tracked.append(change)
    return tracked, untracked, ignored


def head_commit(install_dir: Path, *, runner: Optional[RunFn] = None) -> str:
    result = _run(install_dir, ["rev-parse", "HEAD"], runner=runner, timeout=60.0)
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def current_branch(install_dir: Path, *, runner: Optional[RunFn] = None) -> str:
    result = _run(install_dir, ["rev-parse", "--abbrev-ref", "HEAD"], runner=runner, timeout=60.0)
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def upstream_commit(install_dir: Path, ref: str = "origin/main", *, runner: Optional[RunFn] = None) -> str:
    result = _run(install_dir, ["rev-parse", ref], runner=runner, timeout=60.0)
    return (result.stdout or "").strip() if result.returncode == 0 else ""


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


def classify(install_dir: Path, registry: OverrideRegistry, *, runner: Optional[RunFn] = None) -> OverrideReport:
    """Compare the working tree with the registry: managed / unknown / drifted / missing."""
    report = OverrideReport(registry=registry)
    if registry.broken:
        report.safety = SAFETY_UNKNOWN
        report.reasons_zh.append("overrides 登记表损坏，无法验证本地修改")
        report.reasons_en.append("the override registry is broken: local changes cannot be verified")
        return report
    if not git_available(install_dir=install_dir, runner=runner):
        report.safety = SAFETY_UNKNOWN
        report.reasons_zh.append("找不到可用的 git，无法判断工作区状态")
        report.reasons_en.append("no working git executable: the working tree cannot be classified")
        return report

    changes = detect_changes(install_dir, runner=runner)
    tracked, untracked, ignored = split_changes(changes)
    report.ignored = ignored
    by_path = {change.path: change for change in tracked + untracked}

    for entry in registry.files:
        change = by_path.get(entry.path)
        if entry.status == STATUS_DELETED:
            if not (Path(install_dir) / entry.path).exists():
                # still deleted: this is exactly what was registered
                entry.match = MATCH_EXACT
                report.managed_exact.append(entry.path)
                if change is not None:
                    report.managed.append(change)
            else:
                # the file came back: the deletion override no longer matches
                entry.match = MATCH_DRIFTED
                report.drifted.append(entry.path)
                if change is not None:
                    report.managed.append(change)
            continue

        actual = working_tree_sha256(install_dir, entry.path)
        if not actual and change is None:
            entry.match = MATCH_MISSING
            report.missing.append(entry.path)
            continue
        if actual and actual == entry.current_sha256:
            entry.match = MATCH_EXACT
            report.managed_exact.append(entry.path)
            if change is not None:
                report.managed.append(change)
            continue
        if change is None:
            # the file is back to upstream content: the customization is gone
            entry.match = MATCH_MISSING
            report.missing.append(entry.path)
            continue
        entry.match = MATCH_DRIFTED
        report.drifted.append(entry.path)
        report.managed.append(change)

    registered_paths = set(registry.paths())
    for change in tracked:
        if change.path not in registered_paths and change.path not in report.missing:
            report.unknown.append(change)
    for change in untracked:
        if change.path not in registered_paths:
            # untracked files are never registered implicitly: they may be caches,
            # logs, downloads or build output. They block until the user decides.
            report.unknown.append(change)

    report.safety = _safety_of(report)
    report.reasons_zh, report.reasons_en = _safety_reasons(report)
    return report


def _safety_of(report: OverrideReport) -> str:
    if report.unknown:
        return SAFETY_FAIL
    if report.drifted or report.missing:
        return SAFETY_WARN
    return SAFETY_PASS


def _safety_reasons(report: OverrideReport) -> tuple[list[str], list[str]]:
    zh: list[str] = []
    en: list[str] = []
    if report.managed_count:
        zh.append(f"{report.managed_count} 个已登记的本地定制（{len(report.managed_exact)} 个与登记时一致）")
        en.append(
            f"{report.managed_count} registered local override(s), {len(report.managed_exact)} unchanged since registration"
        )
    if report.drifted:
        zh.append(f"{len(report.drifted)} 个定制在登记之后又被修改过（drift）：先 refresh 或确认")
        en.append(
            f"{len(report.drifted)} override(s) changed again after registration (drift): refresh or confirm first"
        )
    if report.missing:
        zh.append(f"{len(report.missing)} 个登记项在磁盘上已不存在（可能已被你还原）")
        en.append(f"{len(report.missing)} registered override(s) are no longer present on disk (reverted?)")
    if report.unknown:
        zh.append(f"{report.unknown_count} 个未登记修改：更新前必须处理")
        en.append(f"{report.unknown_count} unregistered change(s): handle them before updating")
    if not report.managed_count and not report.unknown_count:
        zh.append("没有本地定制，工作区只有未登记的改动" if report.unknown else "")
    return [line for line in zh if line], [line for line in en if line]


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #


def register_overrides(
    install_dir: Path,
    state_root: Path,
    *,
    paths: Optional[Sequence[str]] = None,
    include_untracked: bool = False,
    base_release: str = "",
    runner: Optional[RunFn] = None,
    stamp: Optional[str] = None,
) -> RegisterResult:
    """Register local changes as managed overrides (never automatic).

    Tracked modifications/added/deleted/renamed files may be registered in bulk.
    Untracked files are only registered when the caller passes ``include_untracked``
    (which the CLI turns into an explicit per-file request).
    """
    result = RegisterResult()
    if not git_available(install_dir=install_dir, runner=runner):
        raise OverrideError("git is not available: cannot register local overrides")
    registry = load_registry(state_root)
    if registry.broken:
        raise OverrideError(f"refusing to write a broken registry: {registry.broken_reason}")

    changes = detect_changes(install_dir, runner=runner)
    tracked, untracked, _ignored = split_changes(changes)
    wanted = {p.replace("\\", "/") for p in paths} if paths else None

    candidates: list[LocalChange] = []
    for change in tracked:
        if wanted is None or change.path in wanted:
            candidates.append(change)
    if wanted is not None:
        for change in untracked:
            if change.path in wanted:
                if not include_untracked:
                    result.refused.append(change.path)
                    result.reasons_zh.append(f"未跟踪文件需要显式指定：{change.path}")
                    result.reasons_en.append(f"untracked file needs an explicit request: {change.path}")
                    continue
                candidates.append(change)
    elif include_untracked:
        candidates.extend(untracked)

    if not candidates:
        result.reasons_zh.append("没有可登记的改动")
        result.reasons_en.append("nothing to register")
        return result

    stamp = stamp or utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
    commit = head_commit(install_dir, runner=runner)
    branch = current_branch(install_dir, runner=runner)
    origin_main = upstream_commit(install_dir, "origin/main", runner=runner)

    tracked_candidates = [c for c in candidates if not c.untracked]
    patch_sha = ""
    patch_file = ""
    if tracked_candidates:
        patch_sha, patch_path = write_patch(
            state_root, stamp, install_dir, [c.path for c in tracked_candidates], runner=runner
        )
        patch_file = str(patch_path)

    for change in candidates:
        entry = registry.entry(change.path) or OverrideEntry(path=change.path)
        existed = registry.entry(change.path) is not None
        entry.status = change.status
        entry.policy = POLICY_PRESERVE
        entry.base_commit = commit
        entry.base_sha256 = blob_sha256(install_dir, change.path, commit or "HEAD", runner=runner) if commit else ""
        entry.current_sha256 = working_tree_sha256(install_dir, change.path)
        entry.patch_sha256 = patch_sha if not change.untracked else ""
        entry.registered_at = iso(utcnow()) or ""
        if change.untracked:
            entry.snapshot_file = str(snapshot_file_for(state_root, stamp, install_dir, change.path))
        else:
            entry.patch_file = patch_file
            entry.snapshot_file = str(snapshot_file_for(state_root, stamp, install_dir, change.path))
        if not existed:
            registry.files.append(entry)
            result.registered.append(change.path)
        else:
            result.refreshed.append(change.path)

    registry.base_commit = registry.base_commit or commit
    registry.base_branch = registry.base_branch or branch
    registry.base_release = base_release or registry.base_release
    registry.origin_main = origin_main or registry.origin_main
    registry.registered_at = iso(utcnow()) or ""
    save_registry(state_root, registry)
    if patch_file:
        result.patch_file = patch_file
        _prune_patches(state_root, keep=10)
    return result


def snapshot_file_for(state_root: Path, stamp: str, install_dir: Path, path: str) -> Path:
    """Copy the current bytes of ``path`` into the override snapshots directory."""
    target = snapshot_path(state_root, stamp, path)
    source = Path(install_dir) / path
    ensure_dir(target.parent)
    try:
        if source.is_file():
            shutil.copy2(source, target)
    except OSError as exc:  # pragma: no cover - defensive
        logger.debug("snapshot of %s failed: %s", path, exc)
    return target


def _prune_patches(state_root: Path, *, keep: int) -> None:
    """Keep the newest ``keep`` patches; older ones are deleted (never the snapshots)."""
    directory = patches_dir(state_root)
    if not directory.is_dir():
        return
    patches = sorted(directory.glob("*.patch"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in patches[max(keep, 1) :]:
        try:
            old.unlink()
        except OSError:  # pragma: no cover
            continue


def unregister_overrides(
    state_root: Path,
    paths: Optional[Sequence[str]] = None,
    *,
    all_entries: bool = False,
    keep_patches: bool = True,
) -> list[str]:
    """Forget registrations. The working tree is never touched."""
    registry = load_registry(state_root)
    if registry.broken:
        raise OverrideError(f"registry is broken: {registry.broken_reason}")
    if all_entries:
        removed = registry.paths()
        registry.files = []
    else:
        wanted = {p.replace("\\", "/") for p in (paths or [])}
        removed = [entry.path for entry in registry.files if entry.path in wanted]
        registry.files = [entry for entry in registry.files if entry.path not in wanted]
    if not keep_patches and not registry.files:
        shutil.rmtree(patches_dir(state_root), ignore_errors=True)
    save_registry(state_root, registry)
    return removed


def refresh_overrides(
    install_dir: Path,
    state_root: Path,
    *,
    paths: Optional[Sequence[str]] = None,
    runner: Optional[RunFn] = None,
) -> RegisterResult:
    """Re-baseline registered overrides: new base commit, new hashes, new patch."""
    registry = load_registry(state_root)
    if registry.broken:
        raise OverrideError(f"registry is broken: {registry.broken_reason}")
    wanted = {p.replace("\\", "/") for p in paths} if paths else None
    targets = [entry.path for entry in registry.files if wanted is None or entry.path in wanted]
    if not targets:
        result = RegisterResult()
        result.reasons_zh.append("没有可刷新的登记项")
        result.reasons_en.append("nothing to refresh")
        return result
    # refresh keeps the same registry entries but re-hashes them against the
    # current working tree, so "drift" becomes the new agreed baseline.
    stamp = utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
    commit = head_commit(install_dir, runner=runner)
    result = RegisterResult()
    tracked_targets = [
        p for p in targets if (Path(install_dir) / p).exists() or _is_tracked(install_dir, p, runner=runner)
    ]
    patch_sha = ""
    patch_file = ""
    if tracked_targets:
        patch_sha, patch_path = write_patch(state_root, stamp, install_dir, tracked_targets, runner=runner)
        patch_file = str(patch_path)
    for entry in registry.files:
        if entry.path not in targets:
            continue
        entry.base_commit = commit or entry.base_commit
        entry.base_sha256 = (
            blob_sha256(install_dir, entry.path, commit or "HEAD", runner=runner) if commit else entry.base_sha256
        )
        entry.current_sha256 = working_tree_sha256(install_dir, entry.path)
        entry.registered_at = iso(utcnow()) or ""
        if entry.path in tracked_targets:
            entry.patch_sha256 = patch_sha
            entry.patch_file = patch_file
        entry.snapshot_file = str(snapshot_file_for(state_root, stamp, install_dir, entry.path))
        result.refreshed.append(entry.path)
    registry.base_commit = commit or registry.base_commit
    registry.registered_at = iso(utcnow()) or ""
    save_registry(state_root, registry)
    result.patch_file = patch_file
    return result


def _is_tracked(install_dir: Path, path: str, *, runner: Optional[RunFn] = None) -> bool:
    result = _run(install_dir, ["ls-files", "--error-unmatch", "--", path], runner=runner, timeout=60.0)
    return result.returncode == 0


# --------------------------------------------------------------------------- #
# conflict prediction
# --------------------------------------------------------------------------- #


def _hunk_ranges(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Old-file line ranges per path from a unified diff."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: Optional[str] = None
    for line in (diff_text or "").splitlines():
        match = _DIFF_PATH_RE.match(line)
        if match:
            current = match.group("b").strip()
            ranges.setdefault(current, [])
            continue
        if line.startswith("@@") and current is not None:
            hunk = _HUNK_RE.match(line)
            if hunk:
                start = int(hunk.group("start"))
                length = int(hunk.group("len") or "1")
                ranges[current].append((start, start + max(length, 1) - 1))
            continue
        if line.startswith("diff --git") and current is not None:
            current = None
    return ranges


def _overlaps(left: list[tuple[int, int]], right: list[tuple[int, int]]) -> bool:
    for a_start, a_end in left:
        for b_start, b_end in right:
            if a_start <= b_end and b_start <= a_end:
                return True
    return False


def _changed_file_set(diff_text: str) -> set[str]:
    return set(_hunk_ranges(diff_text)) | {
        line.split(" b/", 1)[1].strip() for line in (diff_text or "").splitlines() if line.startswith("diff --git a/")
    }


def predict_reapply(
    install_dir: Path,
    registry: OverrideRegistry,
    target: Optional[str],
    *,
    runner: Optional[RunFn] = None,
) -> ReapplyPrediction:
    """Will the registered overrides survive the jump from base to ``target``?

    HIGH   - upstream did not touch those files at all
    MEDIUM - same file, hunks do not overlap
    LOW    - same file, same region: manual merge likely
    UNKNOWN- cannot tell (no target, no git data, binary patches)
    """
    prediction = ReapplyPrediction(target=target or "")
    if not registry.files:
        prediction.confidence = CONFIDENCE_UNKNOWN
        prediction.reasons_zh.append("没有登记项，无需预测")
        prediction.reasons_en.append("nothing registered, nothing to predict")
        return prediction
    if not target:
        prediction.confidence = CONFIDENCE_UNKNOWN
        prediction.reasons_zh.append("没有目标版本：无法预测重新应用")
        prediction.reasons_en.append("no target version: cannot predict reapplication")
        return prediction
    base = registry.base_commit
    if not base:
        prediction.confidence = CONFIDENCE_UNKNOWN
        prediction.reasons_zh.append("登记表里没有 base commit：无法预测")
        prediction.reasons_en.append("the registry has no base commit: cannot predict")
        return prediction
    if not git_available(install_dir=install_dir, runner=runner):
        prediction.confidence = CONFIDENCE_UNKNOWN
        prediction.reasons_zh.append("找不到可用的 git：无法预测")
        prediction.reasons_en.append("no working git executable: cannot predict")
        return prediction

    paths = [entry.path for entry in registry.files]
    upstream = _run(install_dir, ["diff", "--unified=0", base, target, "--", *paths], runner=runner, timeout=240.0)
    if upstream.returncode != 0:
        prediction.confidence = CONFIDENCE_UNKNOWN
        prediction.reasons_zh.append("无法取得上游 diff（目标 commit 可能不在本地对象库）")
        prediction.reasons_en.append(
            "could not diff against the target (its commit may not be in the local object store)"
        )
        return prediction
    upstream_ranges = _hunk_ranges(upstream.stdout or "")
    upstream_by_norm = {_norm(path): ranges for path, ranges in upstream_ranges.items()}
    local_ranges = _hunk_ranges(_registered_patch_text(registry))
    worst = 0  # 0 = HIGH, 1 = MEDIUM, 2 = LOW
    for entry in registry.files:
        path = _norm(entry.path)
        if path not in upstream_by_norm:
            # upstream did not touch this file at all: nothing can conflict
            prediction.per_file[entry.path] = CONFIDENCE_HIGH
            continue
        prediction.upstream_touched.append(entry.path)
        local = local_ranges.get(path) or []
        if not local:
            # no local hunks recorded for this path (untracked snapshot, or the
            # patch could not be read): the same path changing upstream is a risk
            prediction.per_file[entry.path] = CONFIDENCE_MEDIUM
            worst = max(worst, 1)
            continue
        if _overlaps(local, upstream_by_norm[path]):
            prediction.per_file[entry.path] = CONFIDENCE_LOW
            prediction.conflicts.append(entry.path)
            worst = max(worst, 2)
        else:
            prediction.per_file[entry.path] = CONFIDENCE_MEDIUM
            worst = max(worst, 1)
    prediction.confidence = {0: CONFIDENCE_HIGH, 1: CONFIDENCE_MEDIUM, 2: CONFIDENCE_LOW}[worst]
    if prediction.confidence == CONFIDENCE_HIGH:
        prediction.reasons_zh.append("上游没有修改这些文件：重新应用风险低")
        prediction.reasons_en.append("upstream did not touch these files: reapply risk is low")
    elif prediction.confidence == CONFIDENCE_MEDIUM:
        prediction.reasons_zh.append("上游改了同一个文件但区域不重叠：预计能自动应用")
        prediction.reasons_en.append("upstream touched the same files in other regions: automatic reapply expected")
    else:
        prediction.reasons_zh.append("双方修改了同一区域：很可能需要人工合并")
        prediction.reasons_en.append("both sides changed the same region: manual merge likely required")
    return prediction


# small helpers kept private on purpose ------------------------------------- #


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").strip()


def _registered_patch_text(registry: OverrideRegistry) -> str:
    """The local customization diff, straight from the registered patch file."""
    for entry in registry.files:
        if entry.patch_file:
            text = read_patch(Path(entry.patch_file))
            if text:
                return text
    return ""


# --------------------------------------------------------------------------- #
# apply / restore / integrity
# --------------------------------------------------------------------------- #


def apply_overrides(
    install_dir: Path,
    state_root: Path,
    registry: Optional[OverrideRegistry] = None,
    *,
    paths: Optional[Sequence[str]] = None,
    strategy: str = "three_way",
    dry_run: bool = False,
    runner: Optional[RunFn] = None,
) -> ApplyResult:
    """Reapply registered patches onto the current checkout. Never resolves conflicts."""
    registry = registry or load_registry(state_root)
    wanted = {p.replace("\\", "/") for p in paths} if paths else None
    result = ApplyResult(strategy=strategy, dry_run=dry_run)
    patch_files: dict[str, Path] = {}
    for entry in registry.files:
        if wanted is not None and entry.path not in wanted:
            continue
        if entry.patch_file:
            patch_files[entry.patch_file] = Path(entry.patch_file)
    if not patch_files:
        result.failed = []
        return result

    mode = {"three_way": ["--3way"], "check": ["--check"], "plain": []}.get(strategy, ["--3way"])
    for patch_file in patch_files.values():
        if not patch_file.is_file():
            result.failed.append(str(patch_file))
            continue
        args = ["apply", *mode, "--binary", str(patch_file)]
        run = _run(install_dir, args, runner=runner, timeout=300.0)
        text = (run.stdout or "") + (run.stderr or "")
        if run.returncode != 0 and strategy == "three_way":
            # fall back to a plain apply when --3way is unavailable (old git, no blobs)
            fallback = _run(
                install_dir, ["apply", "--check", "--binary", str(patch_file)], runner=runner, timeout=300.0
            )
            if fallback.returncode == 0:
                plain = _run(install_dir, ["apply", "--binary", str(patch_file)], runner=runner, timeout=300.0)
                run = plain
                text = (plain.stdout or "") + (plain.stderr or "")
                result.strategy = "plain"
        if run.returncode != 0:
            result.failed.append(str(patch_file))
            for line in text.splitlines():
                if ".py" in line or ".ts" in line or "error:" in line.lower():
                    result.conflicts.append(line.strip()[:200])
        else:
            result.applied.append(str(patch_file))
        result.output += text
    # untracked-file snapshots are restored by copy (an added file has no patch)
    for entry in registry.files:
        if wanted is not None and entry.path not in wanted:
            continue
        if entry.snapshot_file and not (Path(install_dir) / entry.path).exists():
            target = Path(install_dir) / entry.path
            snapshot = Path(entry.snapshot_file)
            if snapshot.is_file():
                if dry_run:
                    result.applied.append(entry.path + " (would restore)")
                else:
                    ensure_dir(target.parent)
                    shutil.copy2(snapshot, target)
                    result.applied.append(entry.path)
            else:
                result.failed.append(entry.path)
    return result


def restore_clean_state(
    install_dir: Path,
    report: OverrideReport,
    *,
    runner: Optional[RunFn] = None,
    allow_unknown: bool = False,
) -> bool:
    """Return the working tree to upstream state *for the registered paths only*.

    Refuses when anything is unknown (or when asked to skip that check but the
    tree is still unknown): dropping user work to make an update succeed is the
    one thing this tool must never do. ``git reset --hard`` is deliberately not
    used - only the registered paths are restored, everything else is untouched.
    """
    if report.unknown and not allow_unknown:
        return False
    paths = [change.path for change in report.managed if not change.ignored]
    paths = [path for path in paths if path not in {c.path for c in report.unknown}]
    if not paths:
        return True
    tracked = [path for path in paths if _is_tracked(install_dir, path, runner=runner)]
    if tracked:
        run = _run(
            install_dir,
            ["restore", "--source=HEAD", "--staged", "--worktree", "--", *tracked],
            runner=runner,
            timeout=300.0,
        )
        if run.returncode != 0:
            logger.warning("git restore failed: %s", (run.stderr or "").strip()[:200])
            return False
    # deletions registered as overrides: restore the file itself
    for path in tracked:
        entry = report.registry.entry(path) if report.registry else None
        if entry and entry.status == STATUS_DELETED:
            _run(install_dir, ["restore", "--source=HEAD", "--worktree", "--", path], runner=runner, timeout=120.0)
    return True


@dataclass
class IntegrityIssue:
    key: str
    severity: str  # ok | warn | fail | unknown
    message_zh: str
    message_en: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "severity": self.severity,
            "message_zh": self.message_zh,
            "message_en": self.message_en,
        }


def integrity_issues(
    state_root: Path, install_dir: Optional[Path] = None, *, runner: Optional[RunFn] = None
) -> list[IntegrityIssue]:
    """Registry + patch integrity for health/preflight/doctor."""
    issues: list[IntegrityIssue] = []
    registry = load_registry(state_root)
    if registry.broken:
        issues.append(
            IntegrityIssue(
                key="override_registry",
                severity="fail",
                message_zh=f"overrides 登记表损坏：{registry.broken_reason}",
                message_en=f"override registry is broken: {registry.broken_reason}",
            )
        )
        return issues
    if registry.empty:
        issues.append(
            IntegrityIssue(
                key="override_registry", severity="ok", message_zh="没有登记本地定制", message_en="no managed overrides"
            )
        )
        return issues
    issues.append(
        IntegrityIssue(
            key="override_registry",
            severity="ok",
            message_zh=f"登记表可读：{len(registry.files)} 个定制（base {registry.base_commit[:8] or 'unknown'}）",
            message_en=f"registry readable: {len(registry.files)} override(s) (base {registry.base_commit[:8] or 'unknown'})",
        )
    )
    missing_patch = [
        entry.path
        for entry in registry.files
        if entry.patch_file
        and not Path(entry.patch_file).is_file()
        and entry.snapshot_file
        and not Path(entry.snapshot_file).is_file()
    ]
    if missing_patch:
        issues.append(
            IntegrityIssue(
                key="override_patches",
                severity="fail",
                message_zh=f"{len(missing_patch)} 个定制缺少 patch/快照，无法在更新后恢复",
                message_en=f"{len(missing_patch)} override(s) have neither patch nor snapshot: they could not be restored",
            )
        )
    else:
        issues.append(
            IntegrityIssue(
                key="override_patches",
                severity="ok",
                message_zh="patch/snapshot 完整",
                message_en="patches/snapshots present",
            )
        )
    bad_hash = []
    for entry in registry.files:
        if entry.patch_file and Path(entry.patch_file).is_file():
            actual = sha256_bytes(read_patch(Path(entry.patch_file)).encode("utf-8"))
            if entry.patch_sha256 and actual != entry.patch_sha256:
                bad_hash.append(entry.path)
    if bad_hash:
        issues.append(
            IntegrityIssue(
                key="override_patch_hash",
                severity="fail",
                message_zh=f"{len(bad_hash)} 个 patch 的 SHA256 与登记不符（文件被改动过）",
                message_en=f"{len(bad_hash)} patch file(s) no longer match their recorded SHA256",
            )
        )
    else:
        issues.append(
            IntegrityIssue(
                key="override_patch_hash",
                severity="ok",
                message_zh="patch SHA256 校验通过",
                message_en="patch SHA256 verified",
            )
        )
    if install_dir is not None and git_available(install_dir=install_dir, runner=runner):
        report = classify(install_dir, registry, runner=runner)
        severity = {"PASS": "ok", "WARN": "warn", "FAIL": "fail", "UNKNOWN": "unknown"}.get(report.safety, "unknown")
        issues.append(
            IntegrityIssue(
                key="override_status",
                severity=severity,
                message_zh=f"本地定制：{report.managed_count} 个已登记 / {report.unknown_count} 个未登记 / {report.drifted_count} 个 drift",
                message_en=f"local overrides: {report.managed_count} managed / {report.unknown_count} unknown / {report.drifted_count} drifted",
            )
        )
    return issues


def export_overrides(state_root: Path, out_path: Path) -> Path:
    """Bundle registry + patches + snapshots into a zip. Never uploads anything."""
    registry = load_registry(state_root)
    ensure_dir(Path(out_path).parent)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as bundle:
        registry_file = registry_path(state_root)
        if registry_file.is_file():
            bundle.write(registry_file, arcname=f"overrides/{REGISTRY_NAME}")
        for directory, prefix in (
            (patches_dir(state_root), f"overrides/{PATCHES_DIRNAME}"),
            (snapshots_dir(state_root), f"overrides/{SNAPSHOTS_DIRNAME}"),
        ):
            if not directory.is_dir():
                continue
            for item in sorted(directory.rglob("*")):
                if item.is_file():
                    bundle.write(item, arcname=f"{prefix}/{item.relative_to(directory)}")
        bundle.writestr(
            "overrides/export.json",
            json.dumps(
                {
                    "exported_at": iso(utcnow()),
                    "registry_version": registry.version,
                    "entries": len(registry.files),
                    "base_commit": registry.base_commit,
                    "note": "local customization bundle - contains no secrets by design, still do not publish it",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return out_path
