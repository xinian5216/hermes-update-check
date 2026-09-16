"""State files: `update_state.json`, watch state, cache/snapshot directories.

Everything lives under the state dir (default ``~/.hermes-update-check``):

    update_state.json      what we recorded before/after an update (rollback source)
    watch_state.json       what watch mode has already seen and notified about
    cache/                 GitHub responses (TTL)
    snapshots/             config fingerprints taken before an update
    logs/                  rotating log file
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .util import iso, read_json, utcnow, write_json

UPDATE_STATE_NAME = "update_state.json"
WATCH_STATE_NAME = "watch_state.json"

STATUS_IN_PROGRESS = "in_progress"
STATUS_SUCCEEDED = "succeeded"
STATUS_HEALTH_FAILED = "health_check_failed"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_FAILED = "failed"


@dataclass
class UpdateState:
    """Pre-update facts needed to undo an update, plus the outcome.

    This is deliberately richer than "git commit": a git-installed Hermes also
    needs its branch, its venv and (for non-git installs) the backup archive,
    otherwise a rollback leaves the tree and the environment disagreeing.
    """

    previous_version: Optional[str] = None
    previous_tag: Optional[str] = None
    previous_commit: Optional[str] = None
    previous_branch: Optional[str] = None
    previous_dirty: bool = False
    install_kind: str = "unknown"
    install_dir: Optional[str] = None
    hermes_home: Optional[str] = None
    venv_python: Optional[str] = None
    python_version: Optional[str] = None
    uv_path: Optional[str] = None
    backup_path: Optional[str] = None
    snapshot_path: Optional[str] = None
    update_time: Optional[str] = None
    target_version: Optional[str] = None
    target_tag: Optional[str] = None
    command: Optional[str] = None
    status: str = STATUS_IN_PROGRESS
    new_version: Optional[str] = None
    new_commit: Optional[str] = None
    health_summary: Optional[str] = None
    rollback_time: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UpdateState:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})  # type: ignore[arg-type]

    def mark(self, status: str, *, note: Optional[str] = None) -> None:
        self.status = status
        if note:
            self.notes.append(note)

    @property
    def created_at(self) -> Optional[str]:
        return self.update_time

    def human_summary(self, *, lang: str = "zh") -> list[tuple[str, str]]:
        rows = [
            ("previous_version", self.previous_version or "-"),
            ("previous_tag", self.previous_tag or "-"),
            ("previous_commit", self.previous_commit or "-"),
            ("previous_branch", self.previous_branch or "-"),
            ("target_version", self.target_version or "-"),
            ("target_tag", self.target_tag or "-"),
            ("backup_path", self.backup_path or "-"),
            ("update_time", self.update_time or "-"),
            ("status", self.status),
        ]
        return rows


@dataclass
class WatchState:
    """What watch mode remembers between runs, so it can stay silent when nothing changed."""

    last_checked_at: Optional[str] = None
    last_latest_tag: Optional[str] = None
    last_risk_level: Optional[str] = None
    last_risk_score: Optional[int] = None
    last_recommendation: Optional[str] = None
    last_notified_at: Optional[str] = None
    last_notified_tag: Optional[str] = None
    last_notified_reason: Optional[str] = None
    consecutive_failures: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    # -- phase 2: state deltas worth notifying about ------------------------- #
    last_channel: Optional[str] = None
    last_update_status: Optional[str] = None
    last_gate_blocks: list[str] = field(default_factory=list)
    last_critical_clusters: list[str] = field(default_factory=list)
    last_confidence_bucket: Optional[str] = None
    last_overall_level: Optional[str] = None

    MAX_HISTORY = 60

    def record(
        self,
        *,
        tag: Optional[str],
        level: Optional[str],
        score: Optional[int],
        recommendation: Optional[str],
        channel: Optional[str] = None,
        update_status: Optional[str] = None,
        gate_blocks: Optional[list[str]] = None,
        critical_clusters: Optional[list[str]] = None,
        confidence: Optional[int] = None,
    ) -> None:
        self.last_checked_at = iso(utcnow())
        self.last_latest_tag = tag
        self.last_risk_level = level
        self.last_risk_score = score
        self.last_recommendation = recommendation
        if channel is not None:
            self.last_channel = channel
        if update_status is not None:
            self.last_update_status = update_status
        if gate_blocks is not None:
            self.last_gate_blocks = list(gate_blocks)
        if critical_clusters is not None:
            self.last_critical_clusters = list(critical_clusters)
        if confidence is not None:
            self.last_confidence_bucket = confidence_bucket(confidence)
        entry = {
            "checked_at": self.last_checked_at,
            "tag": tag,
            "level": level,
            "score": score,
            "recommendation": recommendation,
            "channel": channel,
            "update_status": update_status,
            "gate_blocks": list(gate_blocks or []),
            "critical_clusters": list(critical_clusters or []),
        }
        self.history.append(entry)
        if len(self.history) > self.MAX_HISTORY:
            self.history = self.history[-self.MAX_HISTORY :]

    def mark_notified(self, tag: Optional[str], reason: str) -> None:
        self.last_notified_at = iso(utcnow())
        self.last_notified_tag = tag
        self.last_notified_reason = reason

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WatchState:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})  # type: ignore[arg-type]


def confidence_bucket(confidence: int) -> str:
    """Coarse confidence bands - small numeric wobbles must not trigger notifications."""
    if confidence >= 85:
        return "HIGH"
    if confidence >= 60:
        return "MEDIUM"
    return "LOW"


class StateStore:
    """Filesystem layout + read/write helpers for all persisted state."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- directories --------------------------------------------------------- #

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def snapshots_dir(self) -> Path:
        return self.root / "snapshots"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "hermes-update-check.log"

    @property
    def update_state_path(self) -> Path:
        return self.root / UPDATE_STATE_NAME

    @property
    def watch_state_path(self) -> Path:
        return self.root / WATCH_STATE_NAME

    def ensure(self) -> None:
        for directory in (self.root, self.cache_dir, self.logs_dir, self.snapshots_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # -- update state -------------------------------------------------------- #

    def load_update_state(self) -> Optional[UpdateState]:
        data = read_json(self.update_state_path)
        if not isinstance(data, dict):
            return None
        try:
            return UpdateState.from_dict(data)
        except TypeError:
            return None

    def save_update_state(self, state: UpdateState) -> Path:
        self.ensure()
        return write_json(self.update_state_path, state.to_dict())

    def clear_update_state(self) -> None:
        try:
            self.update_state_path.unlink()
        except OSError:
            pass

    # -- watch state --------------------------------------------------------- #

    def load_watch_state(self) -> WatchState:
        data = read_json(self.watch_state_path)
        if not isinstance(data, dict):
            return WatchState()
        try:
            return WatchState.from_dict(data)
        except TypeError:
            return WatchState()

    def save_watch_state(self, state: WatchState) -> Path:
        self.ensure()
        return write_json(self.watch_state_path, state.to_dict())

    # -- snapshots ----------------------------------------------------------- #

    def snapshot_dir_for(self, stamp: Optional[str] = None) -> Path:
        stamp = stamp or utcnow().strftime("%Y%m%d-%H%M%S")
        return self.snapshots_dir / stamp
