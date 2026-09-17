"""Config loading/validation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_update_check.config import (
    DEFAULTS,
    Config,
    deep_merge,
    default_config_path,
    load_config,
    resolve_hermes_home,
    resolve_state_dir,
    write_default_config,
)
from hermes_update_check.errors import ConfigError


def test_defaults_are_safe() -> None:
    cfg = Config()
    assert cfg.auto_update is False
    assert cfg.backup_before_update is True
    assert cfg.risk_threshold == 40
    assert cfg.minimum_release_age_days == 5
    assert cfg.check_github_issues is True
    assert cfg.preferred_channel == "stable"
    assert cfg.notify.telegram.enabled is False
    assert cfg.risk.keyword_cap == 40


def test_load_config_missing_file_uses_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "nope.yaml", env={})
    assert cfg.auto_update is False
    assert cfg.source_path is None


def test_yaml_config_overrides(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "risk_threshold: 25\nminimum_release_age_days: 10\nlanguage: en\n"
        "notify:\n  telegram:\n    enabled: true\n    chat_id: '12345'\n",
        encoding="utf-8",
    )
    cfg = load_config(path, env={})
    assert cfg.risk_threshold == 25
    assert cfg.minimum_release_age_days == 10
    assert cfg.language == "en"
    assert cfg.notify.telegram.enabled is True
    assert cfg.notify.telegram.chat_id == "12345"
    # untouched keys keep their default
    assert cfg.backup_before_update is True


def test_json_config_is_supported(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"risk_threshold": 10}), encoding="utf-8")
    cfg = load_config(path, env={})
    assert cfg.risk_threshold == 10


def test_env_overrides_beats_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("risk_threshold: 25\n", encoding="utf-8")
    cfg = load_config(
        path,
        env={
            "HERMES_UPDATE_CHECK_RISK_THRESHOLD": "15",
            "HERMES_UPDATE_CHECK_LANGUAGE": "en",
            "HERMES_UPDATE_CHECK_MINIMUM_RELEASE_AGE_DAYS": "2",
        },
    )
    assert cfg.risk_threshold == 15
    assert cfg.language == "en"
    assert cfg.minimum_release_age_days == 2


def test_unknown_keys_warn(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("risk_thresold: 5\nnotify:\n  telegram:\n    enable: true\n", encoding="utf-8")
    cfg = load_config(path, env={})
    joined = " ".join(cfg.warnings)
    assert "risk_thresold" in joined
    assert "notify.telegram.enable" in joined


def test_validation_clamps_and_warns(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "risk_threshold: 250\nlanguage: klingon\npreferred_channel: nightly\nauto_update: true\n", encoding="utf-8"
    )
    cfg = load_config(path, env={})
    assert cfg.risk_threshold == 100
    assert cfg.language == "zh"
    assert cfg.preferred_channel == "stable"
    assert any("auto_update" in w for w in cfg.warnings)


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("risk_threshold: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_non_mapping_config_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_deep_merge_keeps_nested_defaults() -> None:
    merged = deep_merge(DEFAULTS, {"risk": {"keyword_cap": 10}})
    assert merged["risk"]["keyword_cap"] == 10
    assert merged["risk"]["age_cap"] == DEFAULTS["risk"]["age_cap"]
    assert merged["github"]["repo"] == DEFAULTS["github"]["repo"]


def test_path_resolution_uses_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    monkeypatch.setenv("HERMES_UPDATE_CHECK_STATE_DIR", str(tmp_path / "sd"))
    assert resolve_hermes_home(None) == tmp_path / "hh"
    assert resolve_state_dir(None) == tmp_path / "sd"

    cfg = Config()
    cfg.paths.hermes_home = tmp_path / "cfg-home"
    assert resolve_hermes_home(cfg) == tmp_path / "cfg-home"


def test_default_config_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "custom.yaml"
    monkeypatch.setenv("HERMES_UPDATE_CHECK_CONFIG", str(target))
    assert default_config_path() == target


def test_write_default_config_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "config.yaml"
    written = write_default_config(target)
    assert written.exists()
    cfg = load_config(written, env={})
    assert cfg.auto_update is False
    # writing again without force refuses
    with pytest.raises(ConfigError):
        write_default_config(target)


def test_config_to_dict_serialises_paths(tmp_path: Path) -> None:
    cfg = Config()
    cfg.paths.hermes_home = tmp_path
    data = cfg.to_dict()
    assert isinstance(data["paths"]["hermes_home"], str)
    assert data["auto_update"] is False


# --------------------------------------------------------------------------- #
# phase 2: hard gates
# --------------------------------------------------------------------------- #


def test_hard_gate_defaults() -> None:
    """Phase 3: only the systemic / critical / local-safety rules block by default."""
    cfg = Config()
    gates = cfg.hard_gates
    assert gates.enabled is True
    assert gates.block_on_systemic_risk is True
    assert gates.block_on_critical_workflow is True
    assert gates.block_on_rollback_safety is True
    assert gates.block_dirty_worktree is True
    assert gates.block_on_insufficient_data is True
    # demoted to warnings in phase 3 - these no longer stop an update
    assert gates.block_main_branch_update is False
    assert gates.block_prerelease is False
    assert gates.warn_active_gateway_regression is True
    # the phase-2 knobs survive as deprecated, unset by default
    assert gates.minimum_release_age_hours is None
    assert cfg.release_age_policy.block_hours == 6
    assert cfg.release_age_policy.caution_hours == 12
    assert cfg.release_age_policy.acceptable_hours == 24


def test_hard_gates_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "hard_gates:\n"
        "  enabled: true\n"
        "  minimum_release_age_hours: 12\n"
        "  block_dirty_worktree: false\n"
        "  block_active_gateway_regression: true\n"
        "  block_on_insufficient_data: false\n",
        encoding="utf-8",
    )
    cfg = load_config(path, env={})
    assert cfg.hard_gates.block_dirty_worktree is False
    assert cfg.hard_gates.block_on_insufficient_data is False
    # the legacy key still loads, is reported as deprecated, and maps onto the policy
    assert cfg.hard_gates.minimum_release_age_hours == 12
    assert cfg.release_age_policy.block_hours == 12
    assert any("minimum_release_age_hours" in note for note in cfg.deprecated)
    assert cfg.hard_gates.block_active_gateway_regression is True
    assert cfg.hard_gates.block_on_insufficient_data is False
    # untouched keys keep their (safe) phase-3 defaults
    assert cfg.hard_gates.block_main_branch_update is False
    assert cfg.hard_gates.block_on_systemic_risk is True


def test_hard_gates_enabled_via_env(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "none.yaml", env={})
    assert cfg.hard_gates.enabled is True
    data = cfg.to_dict()
    assert data["hard_gates"]["block_on_systemic_risk"] is True
    assert data["release_age_policy"]["block_hours"] == 6


def test_unknown_hard_gate_key_warns(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("hard_gates:\n  block_dirth_worktree: true\n", encoding="utf-8")
    cfg = load_config(path, env={})
    assert any("block_dirth_worktree" in warning for warning in cfg.warnings)


def test_issue_enrichment_limit_config(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("github:\n  issue_enrichment_limit: 5\n", encoding="utf-8")
    cfg = load_config(path, env={})
    assert cfg.github.issue_enrichment_limit == 5
    assert Config().github.issue_enrichment_limit == 3
