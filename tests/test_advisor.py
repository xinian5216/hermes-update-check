"""Advisor tests: priority order, recheck computation, and 'unknown is not safe'."""

from __future__ import annotations

from datetime import timedelta

from conftest import make_release
from test_clusters import issue
from test_gates import make_assessment, ready_decision, stable_prov

from hermes_update_check.clusters import CONFIDENCE_HIGH, SEVERITY_CRITICAL, RegressionCluster
from hermes_update_check.advisor import (
    RECHECK_CRITICAL_HOURS,
    RECHECK_ROUTINE_HOURS,
    RECOMMEND_AHEAD_OF_STABLE,
    RECOMMEND_MANUAL_REVIEW,
    advise,
    recommended_recheck,
)
from hermes_update_check.clusters import build_clusters
from hermes_update_check.config import Config
from hermes_update_check.gates import EnvironmentState, evaluate_gates
from hermes_update_check.provenance import (
    CHANNEL_MAIN,
    UPDATE_STATUS_AHEAD,
    UPDATE_STATUS_MANUAL_REVIEW,
    UPDATE_STATUS_UNKNOWN,
    UPDATE_STATUS_UP_TO_DATE,
    UpdateDecision,
)
from hermes_update_check.advisor import (
    RECOMMEND_ACCEPTABLE,
    RECOMMEND_BLOCKED,
    RECOMMEND_SAFE,
)
from hermes_update_check.risk import RECOMMEND_UNKNOWN, RECOMMEND_UP_TO_DATE, RECOMMEND_WAIT
from hermes_update_check.util import utcnow



def _cluster(
    key: str, severity: str, confidence: str, *, unavailable: bool = False, feature: str = "sessions"
) -> RegressionCluster:
    """A minimal cluster for readiness tests."""
    evidence = (
        f"{feature} data is unrecoverable after the update"
        if unavailable
        else f"an edge case in {feature} misbehaves"
    )
    return RegressionCluster(
        key=key,
        zh=key,
        en=key,
        severity=severity,
        confidence=confidence,
        reports=3,
        unique_reporters=3,
        open_count=3,
        affected_features=[feature],
        root_causes=3,
        evidence_text=evidence,
    )


def run_advisor(
    cfg: Config,
    *,
    provenance=None,
    decision=None,
    assessment=None,
    clusters=None,
    release=None,
    environment=None,
    readiness=None,
):
    provenance = provenance or stable_prov()
    decision = decision or ready_decision()
    assessment = assessment or make_assessment(score=10)
    clusters = clusters or []
    release = release or make_release(age_hours=200)
    environment = environment if environment is not None else EnvironmentState()
    gates = evaluate_gates(
        cfg,
        provenance=provenance,
        decision=decision,
        release=release,
        assessment=assessment,
        clusters=clusters,
        environment=environment,
        readiness=readiness,
    )
    return advise(
        cfg,
        provenance=provenance,
        decision=decision,
        assessment=assessment,
        gates=gates,
        clusters=clusters,
        release=release,
        readiness=readiness,
    )


# --------------------------------------------------------------------------- #
# priority order
# --------------------------------------------------------------------------- #


def test_up_to_date_wins_over_everything(cfg: Config) -> None:
    decision = UpdateDecision(status=UPDATE_STATUS_UP_TO_DATE, is_update_candidate=False)
    rec = run_advisor(cfg, decision=decision, assessment=make_assessment(score=95), release=make_release(age_hours=1))
    assert rec.action == "UP_TO_DATE"
    assert rec.decided_by == "update_status"


def test_ahead_of_stable_is_reported_as_development_channel_mode(cfg: Config) -> None:
    decision = UpdateDecision(
        status=UPDATE_STATUS_AHEAD, is_update_candidate=False, message_zh="领先 218 个 commit", message_en="218 ahead"
    )
    rec = run_advisor(
        cfg,
        provenance=stable_prov(channel=CHANNEL_MAIN, tag_matched=False, commits_ahead_of_tag=218),
        decision=decision,
    )
    assert rec.action == RECOMMEND_AHEAD_OF_STABLE
    assert any("218" in reason for reason in rec.reasons_en) or any("218" in reason for reason in rec.reasons_zh)
    assert any("downgrade" in step for step in rec.remediation_en)


def test_insufficient_data_beats_gates_and_score(cfg: Config) -> None:
    rec = run_advisor(
        cfg,
        decision=UpdateDecision(status=UPDATE_STATUS_UNKNOWN),
        assessment=make_assessment(insufficient=True),
        release=make_release(age_hours=1),
    )
    assert rec.action == RECOMMEND_UNKNOWN
    assert rec.decided_by == "insufficient_data"
    assert "insufficient" in rec.headline_en.lower()


def test_environment_problem_beats_hard_gates(cfg: Config) -> None:
    env_state = EnvironmentState(
        problems_en=["HERMES_HOME missing"], problems_zh=["HERMES_HOME 不存在"], hermes_home_exists=False
    )
    rec = run_advisor(cfg, release=make_release(age_hours=1), environment=env_state)
    assert rec.action == RECOMMEND_MANUAL_REVIEW
    assert rec.decided_by == "environment"


def test_hard_gate_beats_a_low_score(cfg: Config) -> None:
    """A blocked local state outranks a perfect risk score (phase 3 keeps this)."""
    """A dirty worktree blocks an update even at risk score 8."""
    rec = run_advisor(
        cfg,
        provenance=stable_prov(dirty_worktree=True, dirty_files=3),
        assessment=make_assessment(score=8),
        release=make_release(age_hours=200),
    )
    assert rec.action == RECOMMEND_BLOCKED
    assert rec.decided_by == "dirty_worktree"
    assert "dirty_worktree" in rec.blocking_gates
    assert rec.overall == 8  # the score is still reported, it just does not decide


def test_global_risk_no_longer_decides_on_its_own(cfg: Config) -> None:
    """Phase 3 (doc section 12): a VERY HIGH global score is background information.

    It caps the verdict at ACCEPTABLE (so nothing is called SAFE while the release
    looks globally rough) but it can no longer produce WAIT/AVOID by itself.
    """
    rec = run_advisor(cfg, assessment=make_assessment(score=85), release=make_release(age_hours=200))
    assert rec.action == RECOMMEND_ACCEPTABLE
    assert rec.decided_by == "global_risk"
    assert any("全局风险" in caution for caution in rec.cautions_zh)
    assert rec.overall == 85  # still reported, just not decisive


def test_observation_window_caps_instead_of_blocking(cfg: Config) -> None:
    """Doc section 9: a 3-day-old release in a 5-day window is ACCEPTABLE, not WAIT."""
    cfg.minimum_release_age_days = 5.0
    rec = run_advisor(
        cfg,
        assessment=make_assessment(score=20),
        release=make_release(age_hours=72),  # 3 days
    )
    assert rec.action == RECOMMEND_ACCEPTABLE
    assert any("观察期" in caution for caution in rec.cautions_zh)
    assert rec.recheck_hours is not None and rec.recheck_hours > 1


def test_update_when_everything_is_clean(cfg: Config) -> None:
    rec = run_advisor(cfg, assessment=make_assessment(score=12), release=make_release(age_hours=240))
    assert rec.action == RECOMMEND_SAFE
    assert rec.decided_by == "ok"
    assert rec.recheck_at is not None


def test_readiness_drives_the_verdict(cfg: Config) -> None:
    """Core Feature Readiness - not the global score - decides ACCEPTABLE vs WAIT.

    Two *important* features reported unavailable is enough to drop readiness below
    the acceptable floor, and that (not the risk score) produces the WAIT.
    """
    from hermes_update_check.impact import compute_personal_readiness
    from hermes_update_check.usage_profile import LEVEL_IMPORTANT, UsageProfile

    profile = UsageProfile(
        features={"tools": LEVEL_IMPORTANT, "browser_tools": LEVEL_IMPORTANT}, source="config"
    )
    clusters = [
        _cluster("CRASH", "HIGH", "HIGH", unavailable=True, feature="tools"),
        _cluster("CRASH", "HIGH", "HIGH", unavailable=True, feature="browser_tools"),
    ]
    readiness = compute_personal_readiness(profile, clusters)
    assert readiness.readiness < 60, f"expected a low readiness, got {readiness.readiness}"

    rec = run_advisor(
        cfg,
        assessment=make_assessment(score=10),
        release=make_release(age_hours=240),
        readiness=readiness,
    )
    assert rec.action == RECOMMEND_WAIT
    assert rec.decided_by == "readiness"
    assert rec.core_readiness == readiness.readiness


def test_a_confirmed_unusable_critical_feature_blocks(cfg: Config) -> None:
    """A critical feature that is *confirmed* unavailable goes all the way to BLOCKED.

    The blocker can be the critical-workflow gate or (when the evidence sits in a
    systemic category, as session loss does) the systemic gate - both mean BLOCKED.
    """
    from hermes_update_check.impact import compute_personal_readiness
    from hermes_update_check.usage_profile import LEVEL_CRITICAL, UsageProfile

    profile = UsageProfile(features={"sessions": LEVEL_CRITICAL}, source="config")
    readiness = compute_personal_readiness(profile, [_cluster("SESSION", "CRITICAL", "HIGH", unavailable=True)])
    rec = run_advisor(
        cfg,
        assessment=make_assessment(score=10),
        release=make_release(age_hours=240),
        readiness=readiness,
    )
    assert rec.action == RECOMMEND_BLOCKED
    assert rec.decided_by in {"critical_workflow", "systemic_risk"}
    assert "Session" in " ".join(rec.reasons_zh)


def test_an_unused_feature_regression_changes_nothing(cfg: Config) -> None:
    """Doc section 6: a bug in an unused feature must not move the verdict."""
    from hermes_update_check.impact import compute_personal_readiness
    from hermes_update_check.usage_profile import LEVEL_CRITICAL, LEVEL_UNUSED, UsageProfile

    profile = UsageProfile(features={"sessions": LEVEL_CRITICAL, "docker": LEVEL_UNUSED}, source="config")
    clusters = [_cluster("GATEWAY", "CRITICAL", "HIGH", unavailable=True, feature="docker")]
    readiness = compute_personal_readiness(profile, clusters)
    assert readiness.impact == 0
    assert readiness.readiness == 100

    rec = run_advisor(
        cfg,
        assessment=make_assessment(score=90),
        release=make_release(age_hours=240),
        readiness=readiness,
    )
    # only the global-risk caution remains - never a block from the docker issue
    assert rec.action == RECOMMEND_ACCEPTABLE
    assert rec.decided_by == "global_risk"


def test_manual_review_for_non_standard_state(cfg: Config) -> None:
    rec = run_advisor(cfg, decision=UpdateDecision(status=UPDATE_STATUS_MANUAL_REVIEW))
    assert rec.action == RECOMMEND_MANUAL_REVIEW


# --------------------------------------------------------------------------- #
# recheck computation
# --------------------------------------------------------------------------- #


def test_policy_clearance_drives_the_recheck_for_a_fresh_release(cfg: Config) -> None:
    """A 3-hour-old release blocks on policy; both clocks are reported separately."""
    release = make_release(age_hours=3.0)
    rec = run_advisor(cfg, release=release, assessment=make_assessment(score=12))
    assert rec.action == RECOMMEND_BLOCKED
    assert rec.decided_by == "release_age_policy"
    # next monitoring check = when the blocking condition clears (~3 h)
    assert rec.recheck_hours is not None and 2 <= rec.recheck_hours <= 4
    # and the *policy* clock is reported as its own number (not a promise to update)
    assert rec.policy_clearance_hours is not None and 2 <= rec.policy_clearance_hours <= 4
    assert rec.recheck_at is not None and rec.recheck_at > utcnow()


def test_a_day_old_release_uses_the_routine_interval(cfg: Config) -> None:
    release = make_release(age_hours=14.7)
    rec = run_advisor(cfg, release=release, assessment=make_assessment(score=12))
    assert rec.action == RECOMMEND_ACCEPTABLE  # no more 48 h wall
    assert rec.recheck_hours == RECHECK_ROUTINE_HOURS


def test_recheck_shrinks_with_open_critical_regression(cfg: Config) -> None:
    clusters = build_clusters(
        [
            issue(1, "[Bug] state.db corrupt, data loss", author="alice"),
            issue(2, "[Bug] state.db corrupt after update", author="bob"),
            issue(3, "[Bug] state.db unreadable", author="carol"),
        ]
    )
    rec = run_advisor(cfg, clusters=clusters, assessment=make_assessment(score=30), release=make_release(age_hours=200))
    assert rec.recheck_hours == RECHECK_CRITICAL_HOURS


def test_recheck_helper_prefers_the_soonest_meaningful_moment() -> None:
    now = utcnow()
    hours, _zh, en = recommended_recheck(
        action=RECOMMEND_WAIT,
        gate_deadline=now + timedelta(hours=40),
        clusters=[],
        routine_hours=72,
        now=now,
    )
    assert 39 <= hours <= 41
    assert "clear" in en.lower() or "blocking" in en.lower()

    hours_no_gate, _zh, _en = recommended_recheck(
        action=RECOMMEND_WAIT, gate_deadline=None, clusters=[], routine_hours=72, now=now
    )
    assert hours_no_gate == 72


# --------------------------------------------------------------------------- #
# unknown / missing data must never look safer
# --------------------------------------------------------------------------- #


def test_insufficient_data_never_becomes_update(cfg: Config) -> None:
    """Missing observation data must never be turned into an UPDATE verdict."""
    assessment = make_assessment(score=10)
    assessment.insufficient_data = True
    rec = run_advisor(cfg, assessment=assessment, release=make_release(age_hours=200))
    assert rec.action not in {RECOMMEND_SAFE, RECOMMEND_ACCEPTABLE}
    assert rec.decided_by in {"insufficient_data", "hard_gate"}
    assert rec.blocking_gates or rec.decided_by == "insufficient_data"


def test_lower_bound_score_is_carried_into_the_recommendation(cfg: Config) -> None:
    assessment = make_assessment(score=70)
    assessment.score_is_lower_bound = True
    rec = run_advisor(cfg, assessment=assessment, release=make_release(age_hours=200))
    assert rec.score_is_lower_bound is True
    assert rec.to_dict()["score_is_lower_bound"] is True


def test_recommendation_serialises(cfg: Config) -> None:
    rec = run_advisor(cfg, assessment=make_assessment(score=12), release=make_release(age_hours=240))
    data = rec.to_dict()
    for key in ("action", "decided_by", "overall", "blocking_gates", "recheck_at", "recheck_hours"):
        assert key in data
    assert isinstance(data["reasons_en"], list)
