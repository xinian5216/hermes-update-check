"""CLI-level tests: exit codes, JSON output, commands that never touch the network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeGitHubClient, make_compare, make_release

from hermes_update_check.checker import UpdateCheck, run_check
from hermes_update_check.cli import _check_exit_code, build_parser, main
from hermes_update_check.config import Config
from hermes_update_check.errors import (
    EXIT_CONFIG,
    EXIT_INSUFFICIENT_DATA,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_WAIT,
)
from hermes_update_check.local_env import LocalEnv
from hermes_update_check.risk import RiskAssessment


def test_parser_has_all_documented_commands() -> None:
    parser = build_parser()
    for command in (
        "check",
        "report",
        "watch",
        "update",
        "rollback",
        "health",
        "preflight",
        "config",
        "notify-test",
        "version",
    ):
        assert parser.parse_args([command]) is not None or True
    # unknown commands must fail loudly
    with pytest.raises(SystemExit):
        parser.parse_args(["definitely-not-a-command"])


def test_version_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("HERMES_UPDATE_CHECK_STATE_DIR", str(tmp_path / "state"))
    assert main(["version"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "hermes-update-check" in out


def test_config_path_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("HERMES_UPDATE_CHECK_STATE_DIR", str(tmp_path / "state"))
    config_file = tmp_path / "config.yaml"
    config_file.write_text("risk_threshold: 30\n", encoding="utf-8")
    assert main(["--config", str(config_file), "config", "path"]) == EXIT_OK
    assert str(config_file) in capsys.readouterr().out


def test_config_show_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("HERMES_UPDATE_CHECK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    config_file = tmp_path / "config.yaml"
    config_file.write_text("risk_threshold: 30\nlanguage: en\nauto_update: false\n", encoding="utf-8")
    code = main(["--config", str(config_file), "--json", "config", "show"])
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["risk_threshold"] == 30
    assert payload["language"] == "en"
    assert payload["auto_update"] is False


def test_config_init_writes_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("HERMES_UPDATE_CHECK_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "new-config.yaml"
    assert main(["--config", str(target), "config", "init"]) == EXIT_OK
    assert target.exists()
    assert "risk_threshold" in target.read_text(encoding="utf-8")


def test_invalid_config_returns_config_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_UPDATE_CHECK_STATE_DIR", str(tmp_path / "state"))
    bad = tmp_path / "config.yaml"
    bad.write_text("risk_threshold: [unclosed\n", encoding="utf-8")
    assert main(["--config", str(bad), "config", "show"]) == 3  # EXIT_CONFIG


def test_notify_test_without_channels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_UPDATE_CHECK_STATE_DIR", str(tmp_path / "state"))
    config_file = tmp_path / "config.yaml"
    config_file.write_text("notify:\n  telegram:\n    enabled: false\n", encoding="utf-8")
    assert main(["--config", str(config_file), "notify-test"]) == 3


def test_unknown_command_via_parser() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["nope"])
    assert excinfo.value.code == EXIT_USAGE or excinfo.value.code == 2


# -- exit code mapping ------------------------------------------------------- #


def _check_with(assessment: RiskAssessment | None, update_available: bool | None = True) -> UpdateCheck:
    check = UpdateCheck(cfg=Config(), env=LocalEnv(hermes_home=Path("/tmp/h")))
    check.update_available = update_available
    check.assessment = assessment
    return check


def _assessment(recommendation: str, score: int | None = 10, level: str = "LOW") -> RiskAssessment:
    return RiskAssessment(
        score=score,
        level=level,
        stability=None if score is None else 100 - score,
        recommendation=recommendation,
        confidence=0.9,
    )


@pytest.mark.parametrize(
    "recommendation,score,level,expected",
    [
        ("UPDATE", 10, "LOW", EXIT_OK),  # legacy alias of SAFE
        ("WAIT", 55, "MEDIUM", EXIT_WAIT),
        ("AVOID", 90, "VERY HIGH", EXIT_WAIT),  # legacy alias of BLOCKED
        ("INSUFFICIENT_DATA", None, "UNKNOWN", EXIT_INSUFFICIENT_DATA),
    ],
)
def test_check_exit_codes(recommendation: str, score, level: str, expected: int) -> None:
    assert _check_exit_code(_check_with(_assessment(recommendation, score, level))) == expected


def _check_with_recommendation(action: str):
    from hermes_update_check.advisor import Recommendation

    check = _check_with(_assessment("WAIT", 70, "HIGH"))
    check.recommendation = Recommendation(action=action, decided_by="hard_gate", overall=70)
    return check


@pytest.mark.parametrize(
    "action,expected",
    [
        ("SAFE", EXIT_OK),
        ("ACCEPTABLE", EXIT_OK),  # updating is a reasonable next step, not a promise
        ("BLOCKED", EXIT_WAIT),
        ("WAIT", EXIT_WAIT),
        ("AHEAD_OF_STABLE", EXIT_OK),  # nothing to do: code is ahead of the release
        ("MANUAL_REVIEW", EXIT_WAIT),  # needs a human
        ("INSUFFICIENT_DATA", EXIT_INSUFFICIENT_DATA),
        ("UP_TO_DATE", EXIT_OK),
    ],
)
def test_advisor_action_exit_codes(action: str, expected: int) -> None:
    assert _check_exit_code(_check_with_recommendation(action)) == expected


def test_up_to_date_exit_code_is_zero() -> None:
    assert _check_exit_code(_check_with(_assessment("UPDATE"), update_available=False)) == EXIT_OK


def test_rollback_without_state_is_a_config_error_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Regression (found in the real E2E): `rollback` with no state exited 1.

    A missing precondition is not an internal error - cron/CI must be able to
    tell "you never updated" apart from "the tool crashed".
    """
    monkeypatch.setenv("HERMES_UPDATE_CHECK_STATE_DIR", str(tmp_path / "state"))

    code = main(["--plain", "rollback", "--dry-run"])

    assert code == EXIT_CONFIG
    captured = capsys.readouterr()
    assert "update_state.json" in (captured.out + captured.err)


def test_update_refused_without_yes_on_non_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hermes_home: Path
) -> None:
    """The safety property that matters most: no confirmation -> no update."""
    monkeypatch.setenv("HERMES_UPDATE_CHECK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    releases = [
        make_release(tag="v2026.9.14", version="0.21.3", age_hours=30),
        make_release(tag="v2026.9.11", version="0.21.2", age_hours=100),
    ]
    client = FakeGitHubClient(
        releases=releases,
        compare=make_compare(commits=5),
        search={"created:<": (0, []), 'label:"bug"': (0, []), "in:title": (0, [])},
    )
    monkeypatch.setattr("hermes_update_check.cli.run_check", lambda *a, **kw: run_check(*a, client=client, **kw))
    monkeypatch.setattr("hermes_update_check.cli.run_update", lambda *a, **kw: pytest.fail("update must not run"))
    monkeypatch.setattr(
        "hermes_update_check.cli.detect_local_env",
        lambda *a, **kw: LocalEnv(
            hermes_home=hermes_home, install_kind="git", version="0.21.2", release_tag="v2026.9.11"
        ),
    )
    monkeypatch.setattr(
        "hermes_update_check.cli.run_preflight",
        lambda *a, **kw: __import__("hermes_update_check.preflight", fromlist=["PreflightReport"]).PreflightReport(),
    )

    code = main(["--config", str(tmp_path / "none.yaml"), "update", "--force"])
    assert code != EXIT_OK  # refused: non-interactive without --yes
