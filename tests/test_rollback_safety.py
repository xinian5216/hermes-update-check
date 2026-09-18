"""Rollback safety: the pre-update check that can block *executing* an update."""

from __future__ import annotations

from pathlib import Path

from hermes_update_check.config import Config
from hermes_update_check.local_env import LocalEnv
from hermes_update_check.provenance import CodeProvenance
from hermes_update_check.rollback_safety import (
    SAFETY_FAIL,
    SAFETY_PASS,
    SAFETY_UNKNOWN,
    SAFETY_WARN,
    assess_rollback_safety,
)
from hermes_update_check.state import UpdateState


def make_env(home: Path, *, install_dir: Path | None = None, cli: str = "/usr/bin/hermes") -> LocalEnv:
    env = LocalEnv(hermes_home=home)
    env.install_kind = "git" if install_dir is not None else "pip"
    env.install_dir = install_dir
    env.hermes_cli = cli
    env.venv_python = install_dir / ".venv" / "bin" / "python" if install_dir is not None else None
    return env


def provenance(*, is_git: bool = True) -> CodeProvenance:
    prov = CodeProvenance()
    prov.is_git_install = is_git
    prov.channel = "STABLE" if is_git else "CUSTOM"
    return prov


def state(previous_commit: str = "abc1234") -> UpdateState:
    return UpdateState(previous_commit=previous_commit, previous_tag="v0.21.2")


def test_a_healthy_git_install_passes(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    install = tmp_path / "hermes-agent"
    (install / ".venv" / "bin").mkdir(parents=True)
    (install / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    home.mkdir()
    (home / "backups").mkdir()  # a previous backup exists, so backup support is proven
    state_dir = tmp_path / "state"

    safety = assess_rollback_safety(
        Config(),
        make_env(home, install_dir=install),
        provenance(),
        state=state(),
        state_dir=state_dir,
        git_probe=lambda commit: True,
        disk_free_gib=50.0,
    )
    assert safety.status == SAFETY_PASS
    assert safety.previous_ref == "abc1234"
    assert all(check.status == SAFETY_PASS for check in safety.checks), [
        (check.key, check.status) for check in safety.checks
    ]


def test_backup_not_run_yet_is_unknown_not_pass(tmp_path: Path) -> None:
    """No backups directory yet: honest UNKNOWN (the first update creates it)."""
    home = tmp_path / "hermes"
    install = tmp_path / "hermes-agent"
    install.mkdir(parents=True)
    home.mkdir()
    safety = assess_rollback_safety(
        Config(),
        make_env(home, install_dir=install),
        provenance(),
        state=state(),
        state_dir=tmp_path / "state",
        git_probe=lambda commit: True,
        disk_free_gib=50.0,
    )
    backup_check = next(check for check in safety.checks if check.key == "backup_support")
    assert backup_check.status == SAFETY_UNKNOWN
    assert safety.status in {SAFETY_WARN, SAFETY_UNKNOWN}
    assert safety.status != SAFETY_PASS
    assert safety.failed is False  # unknown does not block, it just is not "proven safe"
    safety.status = SAFETY_UNKNOWN
    assert safety.unsafe is True  # UNKNOWN is never treated as proven safe


def test_an_unreachable_rollback_target_fails(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    install = tmp_path / "hermes-agent"
    install.mkdir(parents=True)
    home.mkdir()

    safety = assess_rollback_safety(
        Config(),
        make_env(home, install_dir=install),
        provenance(),
        state=state("deadbee"),
        state_dir=tmp_path / "state",
        git_probe=lambda commit: False,
        disk_free_gib=50.0,
    )
    assert safety.status == SAFETY_FAIL
    assert safety.failed is True
    assert any("deadbee" in check.detail_en for check in safety.checks if check.key == "commit_recoverable")


def test_a_non_git_install_warns_instead_of_claiming_safety(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    safety = assess_rollback_safety(
        Config(),
        make_env(home, install_dir=None, cli="/usr/bin/hermes"),
        provenance(is_git=False),
        state=None,
        state_dir=tmp_path / "state",
        disk_free_gib=50.0,
    )
    assert safety.status == SAFETY_WARN
    assert any(check.key == "git_install" and check.status == SAFETY_WARN for check in safety.checks)


def test_low_disk_space_warns(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    install = tmp_path / "hermes-agent"
    install.mkdir(parents=True)
    home.mkdir()
    cfg = Config()
    cfg.update.min_free_disk_gib = 2.0

    safety = assess_rollback_safety(
        cfg,
        make_env(home, install_dir=install),
        provenance(),
        state=state(),
        state_dir=tmp_path / "state",
        git_probe=lambda commit: True,
        disk_free_gib=0.4,
    )
    assert safety.status == SAFETY_WARN
    assert any(check.key == "disk_space" and check.status == SAFETY_WARN for check in safety.checks)


def test_an_unwritable_state_dir_fails(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "hermes"
    install = tmp_path / "hermes-agent"
    install.mkdir(parents=True)
    home.mkdir()
    monkeypatch.setattr("hermes_update_check.rollback_safety.os.access", lambda *a, **k: False)

    safety = assess_rollback_safety(
        Config(),
        make_env(home, install_dir=install),
        provenance(),
        state=state(),
        state_dir=tmp_path / "state",
        git_probe=lambda commit: True,
        disk_free_gib=50.0,
    )
    assert safety.status == SAFETY_FAIL
    assert any(check.key == "state_writable" and check.status == SAFETY_FAIL for check in safety.checks)


def test_missing_information_is_unknown_not_pass(tmp_path: Path) -> None:
    """No git checkout, no recorded state: the answer is UNKNOWN, never "safe"."""
    home = tmp_path / "hermes"
    home.mkdir()
    safety = assess_rollback_safety(
        Config(),
        make_env(home, install_dir=None, cli=""),
        provenance(is_git=False),
        state=None,
        state_dir=tmp_path / "state",
        disk_free_gib=None,
    )
    assert safety.status in {SAFETY_UNKNOWN, SAFETY_WARN}
    assert safety.status != SAFETY_PASS
    assert safety.failed is False  # a warning is not a block
    payload = safety.to_dict()
    assert payload["status"] == safety.status
    assert payload["checks"]
