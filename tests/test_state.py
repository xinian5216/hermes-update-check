"""State persistence tests: update_state.json and watch_state.json."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_update_check.state import (
    STATUS_HEALTH_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_ROLLED_BACK,
    STATUS_SUCCEEDED,
    StateStore,
    UpdateState,
    WatchState,
)


def test_state_store_layout(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.ensure()
    assert store.cache_dir.is_dir()
    assert store.logs_dir.is_dir()
    assert store.snapshots_dir.is_dir()
    assert store.update_state_path == tmp_path / "update_state.json"
    assert store.watch_state_path == tmp_path / "watch_state.json"


def test_update_state_roundtrip(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = UpdateState(
        previous_version="0.21.1",
        previous_tag="v2026.9.7",
        previous_commit="deadbeef" * 5,
        previous_branch="main",
        install_kind="git",
        install_dir="/opt/hermes/hermes-agent",
        hermes_home="/home/hermes/.hermes",
        venv_python="/opt/hermes/hermes-agent/venv/bin/python",
        python_version="3.11.9",
        target_version="0.21.3",
        target_tag="v2026.9.14",
        command="hermes update --backup --yes",
        status=STATUS_IN_PROGRESS,
    )
    path = store.save_update_state(state)
    assert path.exists()

    loaded = store.load_update_state()
    assert loaded is not None
    assert loaded.previous_version == "0.21.1"
    assert loaded.previous_commit == "deadbeef" * 5
    assert loaded.target_tag == "v2026.9.14"

    loaded.mark(STATUS_SUCCEEDED, note="health ok")
    store.save_update_state(loaded)
    again = store.load_update_state()
    assert again is not None
    assert again.status == STATUS_SUCCEEDED
    assert "health ok" in again.notes


def test_update_state_matches_documented_json_shape(tmp_path: Path) -> None:
    """The README documents these keys - keep them stable."""
    store = StateStore(tmp_path)
    store.save_update_state(
        UpdateState(previous_version="0.21.1", previous_commit="abc", previous_tag="v2026.9.7", backup_path="/b.zip")
    )
    raw = json.loads((tmp_path / "update_state.json").read_text(encoding="utf-8"))
    for key in (
        "previous_version",
        "previous_commit",
        "previous_tag",
        "backup_path",
        "update_time",
        "status",
        "install_kind",
        "hermes_home",
    ):
        assert key in raw


def test_update_state_unknown_keys_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "update_state.json").write_text(
        json.dumps({"previous_version": "1.2.3", "future_key": 1}), encoding="utf-8"
    )
    state = StateStore(tmp_path).load_update_state()
    assert state is not None and state.previous_version == "1.2.3"


def test_corrupt_state_file_returns_none(tmp_path: Path) -> None:
    (tmp_path / "update_state.json").write_text("{broken", encoding="utf-8")
    assert StateStore(tmp_path).load_update_state() is None


def test_update_state_statuses_and_summary() -> None:
    state = UpdateState(previous_version="0.21.1", status=STATUS_IN_PROGRESS)
    state.mark(STATUS_HEALTH_FAILED, note="doctor failed")
    assert state.status == STATUS_HEALTH_FAILED
    state.status = STATUS_ROLLED_BACK
    rows = dict(state.human_summary())
    assert rows["previous_version"] == "0.21.1"
    assert rows["status"] == STATUS_ROLLED_BACK


def test_watch_state_history_is_capped(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    watch = WatchState()
    for i in range(WatchState.MAX_HISTORY + 15):
        watch.record(tag=f"v2026.9.{i}", level="LOW", score=10, recommendation="UPDATE")
    assert len(watch.history) == WatchState.MAX_HISTORY
    assert watch.last_latest_tag == f"v2026.9.{WatchState.MAX_HISTORY + 14}"

    store.save_watch_state(watch)
    loaded = store.load_watch_state()
    assert loaded.last_latest_tag == watch.last_latest_tag
    assert len(loaded.history) == WatchState.MAX_HISTORY


def test_watch_state_corrupt_file_yields_fresh_state(tmp_path: Path) -> None:
    (tmp_path / "watch_state.json").write_text("not json", encoding="utf-8")
    watch = StateStore(tmp_path).load_watch_state()
    assert watch.last_latest_tag is None


def test_watch_state_mark_notified() -> None:
    watch = WatchState()
    watch.mark_notified("v2026.9.14", "new release")
    assert watch.last_notified_tag == "v2026.9.14"
    assert watch.last_notified_at is not None
