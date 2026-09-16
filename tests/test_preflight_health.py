"""Preflight and health-check tests (local filesystem only)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_update_check.config import Config
from hermes_update_check.health import run_health_checks
from hermes_update_check.local_env import GitState, LocalEnv
from hermes_update_check.preflight import STATUS_FAIL, STATUS_PASS, STATUS_WARN, run_preflight


def make_env(hermes_home: Path, *, dirty: bool = False, install_kind: str = "git") -> LocalEnv:
    return LocalEnv(
        hermes_home=hermes_home,
        install_kind=install_kind,
        install_dir=hermes_home / "hermes-agent",
        hermes_cli=None,
        version="0.21.2",
        release_tag="v2026.9.11",
        python_version="3.11.16",
        git=GitState(
            is_repo=True,
            branch="main",
            commit="5eb99eb2",
            full_commit="5eb99eb2" + "0" * 32,
            dirty=dirty,
            dirty_files=["x.py"] if dirty else [],
        ),
    )


def make_state_db(path: Path, rows: int = 3) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, data TEXT)")
        for i in range(rows):
            conn.execute("INSERT INTO sessions VALUES (?, ?)", (f"s{i}", "{}"))


def test_preflight_happy_path(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    make_state_db(hermes_home / "state.db")
    report = run_preflight(
        cfg,
        make_env(hermes_home),
        state_root=state_root,
        check_processes=False,
        update_help_text="usage: hermes update [--backup] [--plan]",
    )
    statuses = {c.key: c.status for c in report.checks}
    assert statuses["hermes_home"] == STATUS_PASS
    assert statuses["config_file"] == STATUS_PASS
    assert statuses["state_db"] == STATUS_PASS
    assert statuses["backup_support"] == STATUS_PASS
    assert statuses["state_dir"] == STATUS_PASS
    assert statuses["git_clean"] == STATUS_PASS
    assert statuses["disk_space"] in {STATUS_PASS, STATUS_WARN}
    assert report.ok_to_proceed is True


def test_preflight_dirty_tree_warns_but_does_not_block(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    report = run_preflight(
        cfg,
        make_env(hermes_home, dirty=True),
        state_root=state_root,
        check_processes=False,
        update_help_text="--backup",
    )
    git_check = next(c for c in report.checks if c.key == "git_clean")
    assert git_check.status == STATUS_WARN
    assert "uncommitted" in git_check.detail_en
    assert report.ok_to_proceed is True


def test_preflight_missing_hermes_home_blocks(cfg: Config, tmp_path: Path, state_root: Path) -> None:
    missing = tmp_path / "does-not-exist"
    env = LocalEnv(hermes_home=missing, install_kind="unknown")
    report = run_preflight(cfg, env, state_root=state_root, check_processes=False, update_help_text="")
    assert report.ok_to_proceed is False
    assert any(c.key == "hermes_home" and c.status == STATUS_FAIL for c in report.checks)
    assert report.to_dict()["ok_to_proceed"] is False


def test_preflight_detects_broken_state_db(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    (hermes_home / "state.db").write_bytes(b"definitely not a sqlite database")
    report = run_preflight(
        cfg, make_env(hermes_home), state_root=state_root, check_processes=False, update_help_text="--backup"
    )
    db_check = next(c for c in report.checks if c.key == "state_db")
    assert db_check.status == STATUS_FAIL
    assert report.ok_to_proceed is False


def test_preflight_unreadable_config_fails(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    # a directory where a file is expected is unreadable in every OS
    target = hermes_home / "config.yaml"
    target.unlink()
    target.mkdir()
    report = run_preflight(
        cfg, make_env(hermes_home), state_root=state_root, check_processes=False, update_help_text="--backup"
    )
    assert any(c.key == "config_file" and c.status in {STATUS_WARN, STATUS_FAIL} for c in report.checks)


def test_health_checks_on_healthy_home(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    make_state_db(hermes_home / "state.db", rows=5)
    report = run_health_checks(cfg, make_env(hermes_home), state_root=state_root)
    statuses = {c.key: c.status for c in report.checks}
    assert statuses["config_parse"] == STATUS_PASS
    assert statuses["state_db"] == STATUS_PASS
    assert statuses["mcp_tools"] == STATUS_PASS
    # no hermes binary in this sandbox -> the CLI probe must fail, and that is a real failure
    assert statuses["cli_version"] == STATUS_FAIL
    assert report.healthy is False
    assert report.to_dict()["healthy"] is False
    assert report.summary_line(lang="en").count("failure") >= 1


def test_health_state_db_corruption_is_detected(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    (hermes_home / "state.db").write_bytes(b"garbage" * 100)
    report = run_health_checks(cfg, make_env(hermes_home), state_root=state_root)
    db_check = next(c for c in report.checks if c.key == "state_db")
    assert db_check.status == STATUS_FAIL
    assert "SQLite" in db_check.detail_en


def test_health_detects_broken_config_yaml(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    (hermes_home / "config.yaml").write_text("tools: [unclosed\n", encoding="utf-8")
    report = run_health_checks(cfg, make_env(hermes_home), state_root=state_root)
    config_check = next(c for c in report.checks if c.key == "config_parse")
    assert config_check.status == STATUS_FAIL
    assert config_check.remediation_zh


def test_health_missing_state_db_is_a_warning(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    report = run_health_checks(cfg, make_env(hermes_home), state_root=state_root)
    db_check = next(c for c in report.checks if c.key == "state_db")
    assert db_check.status == STATUS_WARN


def test_health_mcp_section_detected(cfg: Config, hermes_home: Path, state_root: Path) -> None:
    (hermes_home / "config.yaml").write_text("mcp:\n  servers:\n    demo:\n      command: echo\n", encoding="utf-8")
    report = run_health_checks(cfg, make_env(hermes_home), state_root=state_root)
    mcp = next(c for c in report.checks if c.key == "mcp_tools")
    assert mcp.status == STATUS_PASS
    assert "mcp" in mcp.detail_en


def _gateway_probe(monkeypatch, output: str, returncode: int = 0) -> None:
    from hermes_update_check import health as health_module
    from hermes_update_check.util import ProcResult

    monkeypatch.setattr(
        health_module,
        "gateway_status",
        lambda *a, **kw: ProcResult(cmd=["hermes", "gateway", "status"], returncode=returncode, stdout=output),
    )


def test_gateway_down_is_warn_when_it_was_never_running(
    cfg: Config, hermes_home: Path, state_root: Path, monkeypatch
) -> None:
    _gateway_probe(monkeypatch, "Gateway is not running\n")
    report = run_health_checks(cfg, make_env(hermes_home), state_root=state_root, gateway_was_running=None)
    gateway = next(c for c in report.checks if c.key == "gateway")
    assert gateway.status == STATUS_WARN
    assert "not running" in gateway.detail_en


def test_gateway_down_after_update_is_a_failure_when_it_ran_before(
    cfg: Config, hermes_home: Path, state_root: Path, monkeypatch
) -> None:
    _gateway_probe(monkeypatch, "Gateway is not running\n")
    report = run_health_checks(cfg, make_env(hermes_home), state_root=state_root, gateway_was_running=True)
    gateway = next(c for c in report.checks if c.key == "gateway")
    assert gateway.status == STATUS_FAIL
    assert report.healthy is False


def test_gateway_running_is_a_pass(cfg: Config, hermes_home: Path, state_root: Path, monkeypatch) -> None:
    _gateway_probe(monkeypatch, "Gateway status: running (pid 1234)\n")
    report = run_health_checks(cfg, make_env(hermes_home), state_root=state_root)
    gateway = next(c for c in report.checks if c.key == "gateway")
    assert gateway.status == STATUS_PASS
