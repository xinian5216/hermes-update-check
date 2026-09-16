"""Updater mechanics: command building, snapshots, dry runs (nothing is executed)."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_update_check.config import Config
from hermes_update_check.errors import AbortedError, CommandError
from hermes_update_check.health import HealthReport
from hermes_update_check.local_env import GitState, LocalEnv
from hermes_update_check.preflight import STATUS_FAIL, STATUS_PASS, CheckResult
from hermes_update_check.state import StateStore, UpdateState
from hermes_update_check.updater import (
    build_update_command,
    create_snapshot,
    dependency_install_command,
    run_rollback,
    run_update,
)
from hermes_update_check.util import ProcResult


def _check(status: str) -> CheckResult:
    return CheckResult(key="k", name_zh="检查", name_en="check", status=status)


@pytest.fixture
def fake_env(tmp_path: Path) -> LocalEnv:
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    return make_env(home)


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    return root


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
        run_rollback(
            cfg, make_env(hermes_home), store=store, yes=True, restore_backup=str(backup), reinstall_deps=False
        )


# --------------------------------------------------------------------------- #
# phase 2+: execution paths (with the outside world stubbed)
# --------------------------------------------------------------------------- #
def test_create_snapshot_fingerprints_and_copies_config(fake_env, state_root) -> None:
    """The snapshot must be able to prove whether config.yaml changed."""
    (fake_env.hermes_home / "config.yaml").write_text("model: k3\n", encoding="utf-8")

    snapshot = create_snapshot(fake_env, state_root)

    by_name = {entry.name: entry for entry in snapshot.entries}
    config = by_name["config.yaml"]
    assert config.exists is True
    assert config.sha256 and len(config.sha256) == 64
    assert config.copied_to and Path(config.copied_to).read_text(encoding="utf-8") == "model: k3\n"

    written = json.loads((snapshot.root / "snapshot.json").read_text(encoding="utf-8"))
    assert written["entries"]
    assert snapshot.db_backup is None  # no state.db in this fixture


def test_create_snapshot_records_missing_entries(fake_env, state_root) -> None:
    snapshot = create_snapshot(fake_env, state_root)
    missing = [e for e in snapshot.entries if not e.exists]
    assert missing  # config.yaml / state.db are absent in the fixture
    assert snapshot.to_dict()["entries"]


def test_dry_run_update_asks_for_the_plan_and_changes_nothing(monkeypatch, cfg, fake_env, state_root) -> None:
    calls: list[list[str]] = []

    def fake_run_process(cmd, **kwargs):
        calls.append([str(c) for c in cmd])
        return ProcResult(cmd=[str(c) for c in cmd], returncode=0, stdout="Update plan:\n  Install: git")

    monkeypatch.setattr("hermes_update_check.updater.run_process", fake_run_process)
    monkeypatch.setattr(
        "hermes_update_check.updater.run_streaming",
        lambda *a, **kw: pytest.fail("a dry run must never execute the update"),
    )

    outcome = run_update(cfg, fake_env, state_root=state_root, dry_run=True)

    assert outcome.dry_run is True
    assert outcome.ok is True
    assert calls and calls[0][-1] == "--plan"
    assert "dry-run" in outcome.message_en


def test_run_update_happy_path_records_state(monkeypatch, cfg, fake_env, state_root) -> None:
    after = dataclasses.replace(make_env(fake_env.hermes_home), version="0.21.3", release_tag="v2026.9.14")
    executed: list[list[str]] = []

    def fake_streaming(cmd, **kwargs):
        executed.append([str(c) for c in cmd])
        return ProcResult(cmd=[str(c) for c in cmd], returncode=0, stdout="updated")

    monkeypatch.setattr("hermes_update_check.updater.run_streaming", fake_streaming)
    monkeypatch.setattr("hermes_update_check.updater.detect_local_env", lambda cfg, **kw: after)
    monkeypatch.setattr(
        "hermes_update_check.updater.run_health_checks",
        lambda *a, **kw: HealthReport(checks=[_check(STATUS_PASS)]),
    )

    outcome = run_update(cfg, fake_env, state_root=state_root, yes=True, backup=True)

    assert outcome.ok is True
    assert executed and executed[0][1] == "update"
    assert outcome.state.previous_version == "0.21.2"
    assert outcome.state.new_version == "0.21.3"
    assert outcome.state.snapshot_path

    saved = StateStore(state_root).load_update_state()
    assert saved is not None and saved.status == "succeeded"


def test_run_update_failure_marks_the_state_and_points_at_rollback(monkeypatch, cfg, fake_env, state_root) -> None:
    monkeypatch.setattr(
        "hermes_update_check.updater.run_streaming",
        lambda *a, **kw: ProcResult(cmd=["hermes", "update"], returncode=2, stderr="boom"),
    )
    monkeypatch.setattr(
        "hermes_update_check.updater.detect_local_env",
        lambda cfg, **kw: dataclasses.replace(make_env(fake_env.hermes_home)),
    )
    monkeypatch.setattr(
        "hermes_update_check.updater.run_health_checks",
        lambda *a, **kw: pytest.fail("health check must not run after a failed update"),
    )

    outcome = run_update(cfg, fake_env, state_root=state_root, yes=True)

    assert outcome.ok is False
    assert "rollback" in outcome.message_en
    saved = StateStore(state_root).load_update_state()
    assert saved is not None and saved.status == "failed"


def test_failed_health_check_can_auto_roll_back(monkeypatch, cfg, fake_env, state_root) -> None:
    from hermes_update_check.updater import RollbackOutcome

    monkeypatch.setattr(
        "hermes_update_check.updater.run_streaming",
        lambda *a, **kw: ProcResult(cmd=["hermes", "update"], returncode=0, stdout="ok"),
    )
    monkeypatch.setattr(
        "hermes_update_check.updater.detect_local_env",
        lambda cfg, **kw: dataclasses.replace(make_env(fake_env.hermes_home), version="0.21.3"),
    )
    monkeypatch.setattr(
        "hermes_update_check.updater.run_health_checks",
        lambda *a, **kw: HealthReport(checks=[_check(STATUS_FAIL)]),
    )
    rolled: list[bool] = []
    monkeypatch.setattr(
        "hermes_update_check.updater.run_rollback",
        lambda *a, **kw: rolled.append(True) or RollbackOutcome(ok=True, steps=["restored"]),
    )

    outcome = run_update(cfg, fake_env, state_root=state_root, yes=True, auto_rollback=True)

    assert outcome.ok is False
    assert outcome.rolled_back is True
    assert rolled == [True]
    assert "auto-rollback" in outcome.message_en


def test_dependency_install_command_falls_back_to_pip(fake_env) -> None:
    fake_env.uv_path = None
    fake_env.venv_python = Path("/opt/hermes/hermes-agent/venv/bin/python")

    command = dependency_install_command(fake_env)

    assert command[0].endswith("python")
    assert command[1:4] == ["-m", "pip", "install"]


def test_dependency_install_command_without_a_venv_is_empty(fake_env) -> None:
    fake_env.uv_path = None
    fake_env.venv_python = None
    assert dependency_install_command(fake_env) == []
