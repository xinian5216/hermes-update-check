"""Advisor tests: priority order, recheck computation, and 'unknown is not safe'."""

from __future__ import annotations

from datetime import timedelta

from conftest import make_release
from test_clusters import issue
from test_gates import make_assessment, ready_decision, stable_prov

from hermes_update_check.advisor import (
    RECHECK_CRITICAL_HOURS,
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
from hermes_update_check.risk import RECOMMEND_AVOID, RECOMMEND_UNKNOWN, RECOMMEND_UPDATE, RECOMMEND_WAIT
from hermes_update_check.util import utcnow


def run_advisor(
    cfg: Config,
    *,
    provenance=None,
    decision=None,
    assessment=None,
    clusters=None,
    release=None,
    environment=None,
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
    )
    return advise(
        cfg,
        provenance=provenance,
        decision=decision,
        assessment=assessment,
        gates=gates,
        clusters=clusters,
        release=release,
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
    """A dirty worktree blocks an update even at risk score 8."""
    rec = run_advisor(
        cfg,
        provenance=stable_prov(dirty_worktree=True, dirty_files=3),
        assessment=make_assessment(score=8),
        release=make_release(age_hours=200),
    )
    assert rec.action == RECOMMEND_WAIT
    assert rec.decided_by == "hard_gate"
    assert "dirty_worktree" in rec.blocking_gates
    assert rec.overall == 8  # the score is still reported, it just does not decide


def test_risk_score_decides_when_gates_pass(cfg: Config) -> None:
    rec = run_advisor(cfg, assessment=make_assessment(score=85), release=make_release(age_hours=200))
    assert rec.action == RECOMMEND_AVOID
    assert rec.decided_by == "risk_score"


def test_cooling_period_after_gates(cfg: Config) -> None:
    """Gates pass (release older than 48 h) but the soft observation window is 5 days."""
    rec = run_advisor(
        cfg,
        assessment=make_assessment(score=20),
        release=make_release(age_hours=72),  # 3 days
    )
    assert rec.action == RECOMMEND_WAIT
    assert rec.decided_by == "cooling_period"
    assert rec.recheck_hours is not None and rec.recheck_hours > 1


def test_update_when_everything_is_clean(cfg: Config) -> None:
    rec = run_advisor(cfg, assessment=make_assessment(score=12), release=make_release(age_hours=240))
    assert rec.action == RECOMMEND_UPDATE
    assert rec.decided_by == "ok"
    assert rec.recheck_at is not None


def test_manual_review_for_non_standard_state(cfg: Config) -> None:
    rec = run_advisor(cfg, decision=UpdateDecision(status=UPDATE_STATUS_MANUAL_REVIEW))
    assert rec.action == RECOMMEND_MANUAL_REVIEW


# --------------------------------------------------------------------------- #
# recheck computation
# --------------------------------------------------------------------------- #


def test_recheck_uses_the_gate_deadline(cfg: Config) -> None:
    release = make_release(age_hours=14.7)
    rec = run_advisor(cfg, release=release, assessment=make_assessment(score=12))
    assert rec.action == RECOMMEND_WAIT
    assert rec.recheck_hours is not None
    # 48h gate - 14.7h elapsed = ~33.3h
    assert 32 <= rec.recheck_hours <= 34
    assert rec.recheck_at is not None
    assert rec.recheck_at > utcnow()


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
    assert "gate" in en.lower()

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
    assert rec.action != RECOMMEND_UPDATE
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
