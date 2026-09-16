"""Hard-gate tests: gates must override a low score and never be bypassed silently."""

from __future__ import annotations

from datetime import timedelta

from conftest import make_release

from hermes_update_check.config import Config
from hermes_update_check.gates import (
    GATE_BLOCK,
    GATE_PASS,
    GATE_SKIP,
    GATE_WARN,
    EnvironmentState,
    evaluate_gates,
    probe_environment,
)
from hermes_update_check.local_env import LocalEnv
from hermes_update_check.provenance import (
    CHANNEL_MAIN,
    CHANNEL_PRERELEASE,
    CHANNEL_STABLE,
    UPDATE_STATUS_AVAILABLE,
    UPDATE_STATUS_UP_TO_DATE,
    CodeProvenance,
    UpdateDecision,
)
from hermes_update_check.risk import RiskAssessment


def make_assessment(*, score: int = 12, confidence: float = 1.0, insufficient: bool = False) -> RiskAssessment:
    return RiskAssessment(
        score=None if insufficient else score,
        level="LOW" if not insufficient else "UNKNOWN",
        stability=None if insufficient else 100 - score,
        recommendation="UPDATE",
        confidence=confidence,
        insufficient_data=insufficient,
        data_confidence=int(confidence * 100),
        overall=None if insufficient else score,
    )


def stable_prov(**kwargs) -> CodeProvenance:
    defaults = {"channel": CHANNEL_STABLE, "tag_matched": True, "nearest_tag": "v2026.9.11", "is_git_install": True}
    defaults.update(kwargs)
    return CodeProvenance(**defaults)  # type: ignore[arg-type]


def ready_decision() -> UpdateDecision:
    return UpdateDecision(status=UPDATE_STATUS_AVAILABLE, is_update_candidate=True, target_tag="v2026.9.14")


def test_gate_blocks_a_young_release(cfg: Config) -> None:
    release = make_release(age_hours=14.7)
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=release,
        assessment=make_assessment(score=12),
        environment=EnvironmentState(),
    )
    age_gate = next(g for g in report.gates if g.key == "release_age")
    assert age_gate.status == GATE_BLOCK
    assert "14.7" in age_gate.reason_en
    assert "48" in age_gate.reason_en
    assert age_gate.earliest_recheck is not None
    # earliest recheck = published + 48h
    assert abs((age_gate.earliest_recheck - release.when).total_seconds() - 48 * 3600) < 5
    assert report.blocked is True


def test_gate_passes_an_old_release(cfg: Config) -> None:
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=12),
        environment=EnvironmentState(),
    )
    assert next(g for g in report.gates if g.key == "release_age").status == GATE_PASS
    assert report.blocked is False


def test_gate_age_threshold_is_configurable(cfg: Config) -> None:
    cfg.hard_gates.minimum_release_age_hours = 6
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=make_release(age_hours=12),
        assessment=make_assessment(score=12),
        environment=EnvironmentState(),
    )
    assert next(g for g in report.gates if g.key == "release_age").status == GATE_PASS


def test_dirty_worktree_blocks_even_at_low_risk(cfg: Config) -> None:
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(dirty_worktree=True, dirty_files=4),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=8),  # risk score is LOW
        environment=EnvironmentState(),
    )
    assert report.blocked_by("dirty_worktree") is True
    assert report.blocked is True


def test_dirty_worktree_can_be_downgraded_to_warning(cfg: Config) -> None:
    cfg.hard_gates.block_dirty_worktree = False
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(dirty_worktree=True),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=8),
        environment=EnvironmentState(),
    )
    assert report.blocked is False
    assert any(g.key == "dirty_worktree" and g.status == GATE_WARN for g in report.gates)


def test_main_branch_blocks(cfg: Config) -> None:
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(channel=CHANNEL_MAIN, tag_matched=False),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=15),
        environment=EnvironmentState(),
    )
    assert report.blocked_by("main_branch") is True


def test_main_branch_can_be_warned_instead(cfg: Config) -> None:
    cfg.hard_gates.block_main_branch_update = False
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(channel=CHANNEL_MAIN, tag_matched=False),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=15),
        environment=EnvironmentState(),
    )
    assert report.blocked_by("main_branch") is False
    assert any(g.key == "main_branch" and g.status == GATE_WARN for g in report.gates)


def test_prerelease_blocks_by_default(cfg: Config) -> None:
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(channel=CHANNEL_PRERELEASE, tag_matched=False),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=15),
        environment=EnvironmentState(),
    )
    assert report.blocked_by("prerelease") is True


def test_active_database_regression_blocks(cfg: Config) -> None:
    from test_clusters import issue

    from hermes_update_check.clusters import build_clusters

    clusters = build_clusters(
        [
            issue(1, "[Bug] state.db corrupt after update", author="alice"),
            issue(2, "[Bug] state.db corrupt, data loss", author="bob"),
        ]
    )
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=20),
        clusters=clusters,
        environment=EnvironmentState(),
    )
    gate = next(g for g in report.gates if g.key == "active_database_regression")
    assert gate.status == GATE_BLOCK
    assert "state.db" in gate.reason_en.lower() or "database" in gate.reason_en.lower()
    assert report.blocked is True


def test_gateway_regression_gate_is_off_by_default_but_available(cfg: Config) -> None:
    from test_clusters import issue

    from hermes_update_check.clusters import build_clusters

    clusters = build_clusters(
        [
            issue(1, "[Bug] gateway fails to start", author="alice"),
            issue(2, "[Bug] gateway crash loop", author="bob"),
        ]
    )
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=20),
        clusters=clusters,
        environment=EnvironmentState(),
    )
    assert next(g for g in report.gates if g.key == "active_gateway_regression").status == GATE_SKIP

    cfg.hard_gates.block_active_gateway_regression = True
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=20),
        clusters=clusters,
        environment=EnvironmentState(),
    )
    assert next(g for g in report.gates if g.key == "active_gateway_regression").status == GATE_BLOCK


def test_single_reporter_cluster_does_not_trigger_the_regression_gate(cfg: Config) -> None:
    from test_clusters import issue

    from hermes_update_check.clusters import build_clusters

    clusters = build_clusters([issue(1, "[Bug] session lost after update", author="alice")])
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=20),
        clusters=clusters,
        environment=EnvironmentState(),
    )
    assert report.blocked_by("active_session_regression") is False


def test_insufficient_data_gate_blocks(cfg: Config) -> None:
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(insufficient=True),
        environment=EnvironmentState(),
    )
    assert report.blocked_by("insufficient_data") is True


def test_missing_release_blocks(cfg: Config) -> None:
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=None,
        assessment=make_assessment(score=10),
        environment=EnvironmentState(),
    )
    assert report.blocked_by("insufficient_data") is True
    assert report.blocked_by("release_age") is True


def test_environment_gate(cfg: Config) -> None:
    env_state = EnvironmentState(
        hermes_home_exists=False,
        problems_zh=["HERMES_HOME 不存在"],
        problems_en=["HERMES_HOME missing"],
    )
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=10),
        environment=env_state,
    )
    assert report.environment_abnormal is True
    assert report.blocked_by("environment") is True


def test_gates_disabled_entirely(cfg: Config) -> None:
    cfg.hard_gates.enabled = False
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(dirty_worktree=True, channel=CHANNEL_MAIN),
        decision=ready_decision(),
        release=make_release(age_hours=1),
        assessment=make_assessment(score=99),
        environment=EnvironmentState(hermes_home_exists=False, problems_en=["x"]),
    )
    assert report.enabled is False
    assert report.blocked is False
    assert report.gates == []


def test_up_to_date_decision_skips_applicable_gates(cfg: Config) -> None:
    decision = UpdateDecision(status=UPDATE_STATUS_UP_TO_DATE, is_update_candidate=False)
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(dirty_worktree=True),
        decision=decision,
        release=make_release(age_hours=2),
        assessment=make_assessment(score=5),
        environment=EnvironmentState(),
    )
    assert report.blocked is False
    assert next(g for g in report.gates if g.key == "release_age").status == GATE_SKIP


def test_channel_mismatch_is_a_warning_gate(cfg: Config) -> None:
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(channel=CHANNEL_MAIN, tag_matched=False),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=15),
        environment=EnvironmentState(),
    )
    mismatch = next(g for g in report.gates if g.key == "channel_mismatch")
    assert mismatch.status == GATE_WARN
    assert "preferred_channel" in mismatch.reason_en


def test_gate_report_serialises() -> None:
    cfg = Config()
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(dirty_worktree=True),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=8),
        environment=EnvironmentState(),
    )
    data = report.to_dict()
    assert data["blocked"] is True
    assert "dirty_worktree" in data["blocking"]
    assert isinstance(data["gates"], list)
    assert data["gates"][0]["label_en"]


def test_probe_environment_detects_problems(hermes_home) -> None:
    ok = probe_environment(LocalEnv(hermes_home=hermes_home, install_kind="git"))
    assert ok.abnormal is False

    missing = probe_environment(LocalEnv(hermes_home=hermes_home / "nope", install_kind="unknown"))
    assert missing.abnormal is True
    assert missing.hermes_home_exists is False
    assert missing.install_kind_known is False


def test_earliest_recheck_is_the_soonest_blocking_deadline(cfg: Config) -> None:
    release = make_release(age_hours=1)
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(dirty_worktree=True),
        decision=ready_decision(),
        release=release,
        assessment=make_assessment(score=9),
        environment=EnvironmentState(),
    )
    deadline = report.earliest_recheck
    assert deadline is not None
    assert deadline <= release.when + timedelta(hours=48) + timedelta(seconds=5)
