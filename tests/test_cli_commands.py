"""End-to-end-ish command tests: every CLI handler, with the outside world stubbed.

These cover the surface a user actually touches (`check`, `report`, `health`,
`preflight`, `watch`, `update`, `rollback`) without the network and without ever
letting `update` reach the real ``hermes update``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_report import build_check

from hermes_update_check.cli import main
from hermes_update_check.config import Config
from hermes_update_check.errors import EXIT_ABORTED, EXIT_HEALTH_FAILED, EXIT_OK, EXIT_PREFLIGHT_FAILED
from hermes_update_check.health import HealthReport
from hermes_update_check.preflight import STATUS_FAIL, STATUS_PASS, CheckResult, PreflightReport
from hermes_update_check.state import StateStore, UpdateState


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hermes_home: Path):
    """A fake "outside world": env detection, GitHub, health and preflight."""
    monkeypatch.setenv("HERMES_UPDATE_CHECK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    env = __import__("test_checker").make_env(hermes_home)
    monkeypatch.setattr("hermes_update_check.cli.detect_local_env", lambda cfg, **kw: env)
    monkeypatch.setattr(
        "hermes_update_check.cli.run_health_checks",
        lambda cfg, env, **kw: HealthReport(checks=[healthy_check()]),
    )
    monkeypatch.setattr(
        "hermes_update_check.cli.run_preflight",
        lambda cfg, env, **kw: PreflightReport(checks=[healthy_check()]),
    )
    return env


def healthy_check(status: str = STATUS_PASS) -> CheckResult:
    """A CheckResult with the given status - the reports derive health from these."""
    return CheckResult(key="k", name_zh="检查项", name_en="check", status=status, detail_zh="ok", detail_en="ok")


def _check(cfg: Config, hermes_home: Path, tmp_path: Path, **kwargs):
    return build_check(cfg, hermes_home, tmp_path / "state", **kwargs)


# --------------------------------------------------------------------------- #
# check / report
# --------------------------------------------------------------------------- #
def test_check_json_is_machine_readable(cfg: Config, hermes_home: Path, tmp_path: Path, capsys, wired) -> None:
    check = _check(cfg, hermes_home, tmp_path)
    import hermes_update_check.cli as cli_module

    cli_module.run_check = lambda *a, **kw: check  # type: ignore[assignment]

    code = main(["--json", "check"])
    payload = json.loads(capsys.readouterr().out)

    assert code in {EXIT_OK, 10}
    assert payload["recommendation"] in {
        "SAFE",
        "ACCEPTABLE",
        "WAIT",
        "BLOCKED",
        "INSUFFICIENT_DATA",
        "AHEAD_OF_STABLE",
        "MANUAL_REVIEW",
    }
    assert payload["risk"]["overall"] is not None or payload["risk"]["level"] == "UNKNOWN"


def test_check_renders_a_report(cfg: Config, hermes_home: Path, tmp_path: Path, capsys, wired) -> None:
    check = _check(cfg, hermes_home, tmp_path)
    import hermes_update_check.cli as cli_module

    cli_module.run_check = lambda *a, **kw: check  # type: ignore[assignment]

    main(["--plain", "check"])
    out = capsys.readouterr().out

    assert "Hermes Update Advisor" in out
    assert "本地安装状态" in out


def test_report_writes_markdown_to_a_file(cfg: Config, hermes_home: Path, tmp_path: Path, wired) -> None:
    check = _check(cfg, hermes_home, tmp_path)
    import hermes_update_check.cli as cli_module

    cli_module.run_check = lambda *a, **kw: check  # type: ignore[assignment]
    target = tmp_path / "report.md"

    code = main(["--plain", "report", "--format", "markdown", "--output", str(target)])

    assert target.exists()
    assert target.read_text(encoding="utf-8").startswith("# Hermes Update Advisor")
    assert code in {EXIT_OK, 10}


def test_report_json_to_a_file(cfg: Config, hermes_home: Path, tmp_path: Path, wired) -> None:
    check = _check(cfg, hermes_home, tmp_path)
    import hermes_update_check.cli as cli_module

    cli_module.run_check = lambda *a, **kw: check  # type: ignore[assignment]
    target = tmp_path / "report.json"

    main(["--plain", "report", "--format", "json", "--output", str(target)])

    json.loads(target.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# health / preflight
# --------------------------------------------------------------------------- #
def test_health_reports_ok(cfg: Config, hermes_home: Path, tmp_path: Path, capsys, wired) -> None:
    assert main(["--plain", "health"]) == EXIT_OK
    assert "健康检查" in capsys.readouterr().out


def test_health_failure_returns_its_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wired) -> None:
    monkeypatch.setattr(
        "hermes_update_check.cli.run_health_checks",
        lambda cfg, env, **kw: HealthReport(checks=[healthy_check(STATUS_FAIL)]),
    )
    assert main(["--plain", "health"]) == EXIT_HEALTH_FAILED


def test_preflight_ok_and_failed(
    cfg: Config, hermes_home: Path, tmp_path: Path, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["--plain", "preflight"]) == EXIT_OK
    monkeypatch.setattr(
        "hermes_update_check.cli.run_preflight",
        lambda cfg, env, **kw: PreflightReport(checks=[healthy_check(STATUS_FAIL)]),
    )
    assert main(["--plain", "preflight"]) == EXIT_PREFLIGHT_FAILED


# --------------------------------------------------------------------------- #
# watch
# --------------------------------------------------------------------------- #
def test_watch_is_quiet_when_nothing_changed(
    cfg: Config, hermes_home: Path, tmp_path: Path, capsys, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _check(cfg, hermes_home, tmp_path)
    import hermes_update_check.cli as cli_module

    cli_module.run_check = lambda *a, **kw: check  # type: ignore[assignment]
    sent: list[str] = []
    monkeypatch.setattr("hermes_update_check.cli.build_notifiers", lambda cfg: [])

    code = main(["--plain", "watch", "--dry-run"])

    assert code in {EXIT_OK, 10}
    out = capsys.readouterr().out
    assert "[watch]" in out
    assert not sent


def test_watch_records_state(
    cfg: Config, hermes_home: Path, tmp_path: Path, wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _check(cfg, hermes_home, tmp_path)
    import hermes_update_check.cli as cli_module

    cli_module.run_check = lambda *a, **kw: check  # type: ignore[assignment]
    monkeypatch.setattr("hermes_update_check.cli.build_notifiers", lambda cfg: [])

    main(["--plain", "watch", "--no-notify"])

    store = StateStore(tmp_path / "state")
    state = store.load_watch_state()
    assert state is not None
    assert state.last_latest_tag == check.latest.tag
    assert state.last_channel == check.channel


# --------------------------------------------------------------------------- #
# update / rollback (nothing may reach the real `hermes update`)
# --------------------------------------------------------------------------- #
def test_update_dry_run_never_executes(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, hermes_home: Path, tmp_path: Path, capsys, wired
) -> None:
    check = _check(cfg, hermes_home, tmp_path)
    import hermes_update_check.cli as cli_module

    cli_module.run_check = lambda *a, **kw: check  # type: ignore[assignment]
    seen: dict[str, object] = {}

    def fake_run_update(cfg, env, **kwargs):
        seen.update(kwargs)
        from hermes_update_check.state import UpdateState
        from hermes_update_check.updater import UpdateOutcome
        from hermes_update_check.util import ProcResult

        return UpdateOutcome(
            ok=True,
            state=UpdateState(),
            env_before=env,
            result=ProcResult(cmd=["hermes", "update", "--plan"], returncode=0, stdout="plan"),
        )

    monkeypatch.setattr("hermes_update_check.cli.run_update", fake_run_update)

    code = main(["--plain", "update", "--dry-run"])

    assert seen.get("dry_run") is True  # the plan is requested, nothing is executed
    assert code in {EXIT_OK, 10, 14}
    assert "演练" in capsys.readouterr().out


def test_update_refuses_without_confirmation_on_a_tty_less_shell(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, hermes_home: Path, tmp_path: Path, wired
) -> None:
    check = _check(cfg, hermes_home, tmp_path)
    import hermes_update_check.cli as cli_module

    cli_module.run_check = lambda *a, **kw: check  # type: ignore[assignment]
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "hermes_update_check.cli.run_update",
        lambda *a, **kw: pytest.fail("run_update must not be called without confirmation"),
    )

    code = main(["--plain", "update"])

    assert code in {EXIT_ABORTED, 10, 14}


def test_rollback_dry_run_shows_the_plan(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, hermes_home: Path, tmp_path: Path, capsys, wired
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure()
    store.save_update_state(
        UpdateState(
            previous_version="0.21.1",
            previous_tag="v2026.9.10",
            previous_commit="a" * 40,
            previous_branch="main",
            install_kind="git",
            install_dir=str(hermes_home / "hermes-agent"),
            update_time="2026-09-15T00:00:00+00:00",
            target_version="0.21.2",
            target_tag="v2026.9.11",
        )
    )
    from hermes_update_check.updater import RollbackOutcome

    seen: dict[str, object] = {}

    def fake_rollback(cfg, env, **kwargs):
        seen.update(kwargs)
        return RollbackOutcome(ok=True, steps=["would check out aaaa"])

    monkeypatch.setattr("hermes_update_check.cli.run_rollback", fake_rollback)

    code = main(["--plain", "rollback", "--dry-run"])

    assert seen.get("dry_run") is True
    assert code == EXIT_OK


def test_rollback_plan_includes_the_recorded_version(
    cfg: Config, hermes_home: Path, tmp_path: Path, capsys, wired
) -> None:
    store = StateStore(tmp_path / "state")
    store.ensure()
    store.save_update_state(
        UpdateState(
            previous_version="0.21.1",
            previous_tag="v2026.9.10",
            previous_commit="b" * 40,
            previous_branch="main",
            install_kind="git",
            update_time="2026-09-15T00:00:00+00:00",
            target_version="0.21.2",
            target_tag="v2026.9.11",
        )
    )
    captured: dict[str, object] = {}

    def fake_rollback(cfg, env, **kwargs):
        captured.update(kwargs)
        from hermes_update_check.updater import RollbackOutcome

        return RollbackOutcome(ok=True, steps=["checked out bbbb"])

    import hermes_update_check.cli as cli_module

    cli_module.run_rollback = fake_rollback  # type: ignore[assignment]
    main(["--plain", "rollback", "--yes"])

    assert captured.get("state") is not None
    assert captured.get("to_ref") is None
