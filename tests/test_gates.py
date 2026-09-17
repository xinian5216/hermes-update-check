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


def test_gate_blocks_only_a_very_young_release(cfg: Config) -> None:
    """Phase 3 (doc section 9): < 6 h blocks, 6-12 h cautions, the rest passes.

    The old 48-hour wall is gone: a release that is a day old no longer waits.
    """
    fresh = make_release(age_hours=3.0)
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=fresh,
        assessment=make_assessment(score=12),
        environment=EnvironmentState(),
    )
    age_gate = next(g for g in report.gates if g.key == "release_age")
    assert age_gate.status == GATE_BLOCK
    assert "3.0" in age_gate.reason_en
    assert age_gate.earliest_recheck is not None
    # earliest policy clearance = published + block_hours
    assert abs((age_gate.earliest_recheck - fresh.when).total_seconds() - 6 * 3600) < 5
    assert report.blocked is True
    assert report.age_band == "block"


def test_caution_band_warns_instead_of_blocking(cfg: Config) -> None:
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=make_release(age_hours=8.0),
        assessment=make_assessment(score=12),
        environment=EnvironmentState(),
    )
    age_gate = next(g for g in report.gates if g.key == "release_age")
    assert age_gate.status == GATE_WARN
    assert age_gate.caps_verdict is True  # caps the verdict at ACCEPTABLE, never blocks
    assert report.blocked is False
    assert report.age_band == "caution"
    assert report.caution_clearance is not None


def test_a_day_old_release_is_no_longer_held_back(cfg: Config) -> None:
    """The exact release that made the old model say WAIT for 48 h."""
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=make_release(age_hours=14.7),
        assessment=make_assessment(score=12),
        environment=EnvironmentState(),
    )
    assert next(g for g in report.gates if g.key == "release_age").status == GATE_PASS
    assert report.blocked is False
    assert report.age_band == "acceptable"


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


def test_main_branch_warns_by_default(cfg: Config) -> None:
    """Phase 3 (doc section 10): tracking main raises Environment Risk, it does not block."""
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(channel=CHANNEL_MAIN, tag_matched=False),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=15),
        environment=EnvironmentState(),
    )
    gate = next(g for g in report.gates if g.key == "main_branch")
    assert gate.status == GATE_WARN
    assert gate.caps_verdict is True
    assert report.blocked_by("main_branch") is False
    assert report.blocked is False


def test_main_branch_can_still_be_configured_to_block(cfg: Config) -> None:
    """An explicit opt-in keeps the old, stricter behaviour available."""
    cfg.hard_gates.block_main_branch_update = True
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(channel=CHANNEL_MAIN, tag_matched=False),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=15),
        environment=EnvironmentState(),
    )
    assert report.blocked_by("main_branch") is True


def test_prerelease_warns_by_default_and_can_still_block(cfg: Config) -> None:
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(channel=CHANNEL_PRERELEASE, tag_matched=False),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=15),
        environment=EnvironmentState(),
    )
    gate = next(g for g in report.gates if g.key == "prerelease")
    assert gate.status == GATE_WARN
    assert report.blocked is False

    cfg.hard_gates.block_prerelease = True
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(channel=CHANNEL_PRERELEASE, tag_matched=False),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=15),
        environment=EnvironmentState(),
    )
    assert report.blocked_by("prerelease") is True


def test_state_db_corruption_blocks_through_the_systemic_gate(cfg: Config) -> None:
    """Phase 3: data corruption still blocks - but as a *systemic* risk, and only
    when the evidence is corroborated (2 independent reporters + maintainer triage)."""
    from test_clusters import issue

    from hermes_update_check.clusters import build_clusters
    from hermes_update_check.impact import compute_personal_readiness
    from hermes_update_check.usage_profile import builtin_default_profile

    clusters = build_clusters(
        [
            issue(
                1,
                "[Bug] state.db corrupt after update - data loss",
                author="alice",
                labels=["bug", "confirmed"],
                body="Steps to reproduce: run hermes update, then open a session.",
            ),
            issue(
                2,
                "[Bug] state.db corrupt, data loss on upgrade",
                author="bob",
                labels=["bug"],
                body="Steps to reproduce: upgrade from v0.21.2 to v0.21.3.",
            ),
        ]
    )
    readiness = compute_personal_readiness(builtin_default_profile(), clusters, extra_systemic=())
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=20),
        clusters=clusters,
        environment=EnvironmentState(),
        readiness=readiness,
    )
    gate = next(g for g in report.gates if g.key == "systemic_risk")
    assert gate.status == GATE_BLOCK
    assert readiness.blocking_systemic, "DATA_CORRUPTION should be blocking"
    assert report.blocked is True
    # the old per-cluster blocking gate is gone; the class is a *warning* now
    assert not any(g.key == "active_database_regression" for g in report.gates)


def test_an_uncorroborated_corruption_report_only_warns(cfg: Config) -> None:
    """A single unconfirmed report is not enough to block (doc: high confidence only)."""
    from test_clusters import issue

    from hermes_update_check.clusters import build_clusters
    from hermes_update_check.impact import compute_personal_readiness
    from hermes_update_check.usage_profile import builtin_default_profile

    clusters = build_clusters([issue(9, "[Bug] state.db corrupt on my machine", author="alice")])
    readiness = compute_personal_readiness(builtin_default_profile(), clusters, extra_systemic=())
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=20),
        clusters=clusters,
        environment=EnvironmentState(),
        readiness=readiness,
    )
    assert readiness.active_systemic  # the evidence is visible...
    assert not readiness.blocking_systemic  # ...but not corroborated
    assert report.blocked_by("systemic_risk") is False
    assert next(g for g in report.gates if g.key == "systemic_risk").status == GATE_WARN


def test_gateway_regression_warns_and_never_blocks_by_itself(cfg: Config) -> None:
    """Phase 3 (doc section 8): an ordinary gateway regression is a warning.

    It still shows up (Environment Risk + readiness cost for anyone who runs a
    gateway) but it cannot stop the update on its own.
    """
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
    gate = next(g for g in report.gates if g.key == "warn_gateway_regression")
    assert gate.status == GATE_WARN
    assert gate.caps_verdict is True
    assert report.blocked is False

    cfg.hard_gates.warn_active_gateway_regression = False
    report = evaluate_gates(
        cfg,
        provenance=stable_prov(),
        decision=ready_decision(),
        release=make_release(age_hours=200),
        assessment=make_assessment(score=20),
        clusters=clusters,
        environment=EnvironmentState(),
    )
    assert next(g for g in report.gates if g.key == "warn_gateway_regression").status == GATE_SKIP


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
    # an unknown publication date is a warning now (the insufficient-data gate blocks)
    assert report.blocked_by("release_age") is False
    assert next(g for g in report.gates if g.key == "release_age").status == GATE_WARN


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
