"""Updater mechanics: command building, snapshots, dry runs (nothing is executed)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_update_check.config import Config
from hermes_update_check.errors import AbortedError, CommandError
from hermes_update_check.local_env import GitState, LocalEnv
from hermes_update_check.state import StateStore, UpdateState
from hermes_update_check.updater import (
    build_update_command,
    create_snapshot,
    dependency_install_command,
    run_rollback,
    run_update,
)


def make_env(hermes_home: Path, *, uv: bool = True, venv: bool = True) -> LocalEnv:
    return LocalEnv(
        hermes_home=hermes_home,
        install_kind="git",
        install_dir=hermes_home / "hermes-agent",
        hermes_cli="hermes",
        version="0.21.2",
        release_tag="v2026.9.11",
        python_version="3.11.16",
        uv_path="/usr/local/bin/uv" if uv else None,
        venv_python=(hermes_home / "hermes-agent" / "venv" / "bin" / "python") if venv else None,
        git=GitState(
            is_repo=True,
            branch="main",
            commit="5eb99eb2",
            full_commit="5eb99eb2" + "0" * 32,
            dirty=False,
        ),
    )


def test_build_update_command_defaults() -> None:
    cfg = Config()
    env = LocalEnv(
        hermes_home=Path("/tmp/h"),
        install_kind="git",
        hermes_cli="hermes",
        git=GitState(is_repo=True, branch="main", commit="abc1234"),
    )
    cmd = build_update_command(cfg, env, backup=True, yes=True, branch="main")
    assert cmd[:3] == ["hermes", "update", "--backup"]
    assert "--yes" in cmd
    assert cmd[cmd.index("--branch") + 1] == "main"


def test_build_update_command_no_backup_and_extra_args() -> None:
    cfg = Config()
    env = LocalEnv(hermes_home=Path("/tmp/h"), install_kind="pip", hermes_cli="/opt/hermes")
    cmd = build_update_command(cfg, env, backup=False, yes=False, branch="main", extra_args=["--force-venv"])
    assert cmd[0] == "/opt/hermes"
    assert "--no-backup" in cmd
    assert "--yes" not in cmd
    assert "--branch" not in cmd  # not a git install
    assert "--force-venv" in cmd


def test_build_update_command_without_cli_raises() -> None:
    cfg = Config()
    env = LocalEnv(hermes_home=Path("/tmp/h"), install_kind="unknown", hermes_cli=None)
    with pytest.raises(CommandError):
        build_update_command(cfg, env, backup=True, yes=False)


def test_snapshot_fingerprints_and_copies_config(hermes_home: Path, state_root: Path) -> None:
    (hermes_home / ".env").write_text("SECRET=1\n", encoding="utf-8")
    with sqlite3.connect(hermes_home / "state.db") as conn:
        conn.execute("CREATE TABLE sessions (id TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('a')")

    env = make_env(hermes_home)
    snapshot = create_snapshot(env, state_root)

    assert snapshot.root.exists()
    manifest = json.loads((snapshot.root / "snapshot.json").read_text(encoding="utf-8"))
    names = {entry["name"]: entry for entry in manifest["entries"]}
    assert names["config.yaml"]["copied_to"] is not None
    assert names["config.yaml"]["sha256"]
    assert (snapshot.root / "config.yaml").exists()
    # secrets are hashed but never copied
    assert names[".env"]["copied_to"] is None
    assert (snapshot.root / ".env").exists() is False
    # the sqlite backup is a real, openable database
    assert snapshot.db_backup is not None
    with sqlite3.connect(snapshot.db_backup) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_snapshot_tolerates_missing_files(hermes_home: Path, state_root: Path) -> None:
    env = make_env(hermes_home)
    snapshot = create_snapshot(env, state_root)
    entries = {e.name: e for e in snapshot.entries}
    assert entries["auth.json"].exists is False
    assert entries["config.yaml"].exists is True


def test_dependency_install_command_prefers_uv(hermes_home: Path) -> None:
    env = make_env(hermes_home, uv=True)
    cmd = dependency_install_command(env)
    assert cmd[0] == "/usr/local/bin/uv"
    assert cmd[1:4] == ["pip", "install", "-e"]
    assert ".[all]" in cmd

    env_no_uv = make_env(hermes_home, uv=False)
    cmd2 = dependency_install_command(env_no_uv)
    assert cmd2[1:4] == ["-m", "pip", "install"]


def test_run_update_dry_run_does_not_touch_anything(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    env = make_env(hermes_home)
    outcome = run_update(cfg, env, state_root=state_root, dry_run=True)
    assert outcome.dry_run is True
    assert not (state_root / "update_state.json").exists()
    assert not any((state_root / "snapshots").glob("*")) if (state_root / "snapshots").exists() else True


def test_run_rollback_requires_confirmation_and_state(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    store = StateStore(state_root)
    env = make_env(hermes_home)
    with pytest.raises(CommandError):
        run_rollback(cfg, env, store=store)  # no recorded state

    store.save_update_state(UpdateState(previous_commit="abc123", install_dir=str(hermes_home / "hermes-agent")))
    with pytest.raises(AbortedError):
        run_rollback(cfg, env, store=store, yes=False)


def test_run_rollback_dry_run_plan(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    store = StateStore(state_root)
    env = make_env(hermes_home)
    store.save_update_state(
        UpdateState(
            previous_commit="abc123" + "0" * 34,
            previous_tag="v2026.9.11",
            install_dir=str(hermes_home / "hermes-agent"),
            hermes_home=str(hermes_home),
            install_kind="git",
        )
    )
    outcome = run_rollback(cfg, env, store=store, yes=True, dry_run=True)
    assert outcome.dry_run is True
    assert any("git checkout" in step for step in outcome.steps)
    assert any("dependencies" in step for step in outcome.steps)
    # dry-run must not have modified the persisted state
    assert store.load_update_state().status == "in_progress"


def test_run_rollback_without_git_repo_raises(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    store = StateStore(state_root)
    env = make_env(hermes_home)
    env.install_kind = "pip"
    (hermes_home / "not-a-repo").mkdir(exist_ok=True)
    store.save_update_state(UpdateState(previous_commit="abc123", install_dir=str(hermes_home / "not-a-repo")))
    with pytest.raises(CommandError):
        run_rollback(cfg, env, store=store, yes=True)


def test_rollback_with_backup_path_but_no_commit(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    store = StateStore(state_root)
    backup = hermes_home / "backups" / "hermes-backup.zip"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(b"not a zip")
    store.save_update_state(UpdateState(previous_version="0.21.1", hermes_home=str(hermes_home), install_kind="pip"))
    with pytest.raises(CommandError):
        # no recorded ref and the "backup" is not a real archive: refuse loudly
        run_rollback(cfg, make_env(hermes_home), store=store, yes=True, restore_backup=str(backup), reinstall_deps=False)
