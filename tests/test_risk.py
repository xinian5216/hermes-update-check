"""Risk-engine tests: the scoring rules are the product, so they are pinned here."""

from __future__ import annotations

import pytest

from hermes_update_check.config import RiskWeights
from hermes_update_check.github_api import Issue
from hermes_update_check.risk import (
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_LOW_MEDIUM,
    LEVEL_MEDIUM,
    LEVEL_UNKNOWN,
    LEVEL_VERY_HIGH,
    RECOMMEND_AVOID,
    RECOMMEND_UNKNOWN,
    RECOMMEND_UPDATE,
    RECOMMEND_WAIT,
    UNKNOWN_REGRESSION_FLOOR,
    CheckContext,
    IssueSignal,
    RiskAssessment,
    age_points,
    assess,
    bonus_points,
    classify_issues,
    commit_volume_points,
    context_points,
    issue_points,
    keyword_points,
    level_for_score,
    release_type_points,
    rollup_points,
    scan_keywords,
)

WEIGHTS = RiskWeights()


def make_issue_signal(**kwargs) -> IssueSignal:  # noqa: ANN003
    defaults = dict(window_days=3.0, post_release_total=0, baseline_total=0, baseline_window_days=3.0)
    defaults.update(kwargs)
    return IssueSignal(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# keyword scan
# --------------------------------------------------------------------------- #


def test_keyword_scan_detects_database_and_session() -> None:
    body = """
## What this patch ships for you
- fix(state): second-process maintenance on state.db refuses ANY foreign holder
- fix(sessions): never lose a transcript on migration
- The database schema migration is now idempotent.
"""
    scan = scan_keywords(body)
    keys = {hit.key for hit in scan.hits}
    assert "database" in keys
    assert "session_state" in keys
    assert scan.raw_points > 0
    database = next(h for h in scan.hits if h.key == "database")
    assert database.hits >= 1


def test_keyword_scan_breaking_change_is_the_heaviest() -> None:
    scan = scan_keywords("BREAKING CHANGE: the tools API was rewritten and config format changed.")
    keys = {hit.key for hit in scan.hits}
    assert "breaking" in keys
    assert "rewrite" in keys


def test_keyword_scan_low_risk_markers() -> None:
    scan = scan_keywords("Documentation and UI polish, typo fixes in the README, new model support.")
    assert scan.total_hits == 0
    assert scan.low_risk_hits > 0
    assert keyword_points(scan, WEIGHTS.keyword_cap) == 0.0


def test_keyword_points_are_capped() -> None:
    corpus = " ".join(
        [
            "breaking change", "database migration", "state.db", "sqlite schema",
            "session store", "gateway", "authentication token credential oauth",
            "config migration", "rewrite refactor overhaul", "storage serialization backend",
            "memory", "provider sdk api mcp tool system",
        ]
    ) * 20
    scan = scan_keywords(corpus)
    points = keyword_points(scan, WEIGHTS.keyword_cap)
    assert points <= WEIGHTS.keyword_cap
    assert points >= WEIGHTS.keyword_cap * 0.9


def test_keyword_points_scale_down_for_a_single_mention() -> None:
    scan = scan_keywords("database")
    points = keyword_points(scan, WEIGHTS.keyword_cap)
    assert 0 < points < WEIGHTS.keyword_cap * 0.5


def test_keyword_intensity_grows_with_hits() -> None:
    one = scan_keywords("database")
    many = scan_keywords("database\n" * 20)
    assert keyword_points(many, 40) > keyword_points(one, 40)


# --------------------------------------------------------------------------- #
# individual factors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "age_hours,expected_fraction",
    [(1, 1.0), (23, 1.0), (48, 13 / 18), (100, 8 / 18), (200, 3 / 18), (500, 0.0), (1000, -5 / 18)],
)
def test_age_points(age_hours: float, expected_fraction: float) -> None:
    points, _reason = age_points(age_hours, 18.0)
    assert points == pytest.approx(18.0 * expected_fraction)


def test_age_points_unknown_is_not_free() -> None:
    points, reason = age_points(None, 18.0)
    assert points == pytest.approx(9.0)
    assert reason == "age unknown"


def test_commit_volume_and_pr_volume() -> None:
    assert commit_volume_points(3, 18.0)[0] == 0.0
    assert commit_volume_points(1000, 18.0)[0] > commit_volume_points(100, 18.0)[0]
    assert commit_volume_points(None, 18.0)[0] > 0
    assert release_type_points(latest_version="0.22.0", current_version="0.21.3", cap=18.0)[0] > 0
    assert release_type_points(latest_version="0.21.3", current_version="0.21.2", cap=18.0)[0] == 0.0
    assert rollup_points("This tag rolls up the ~338 PRs merged since the last tag", 1039, 18.0)[0] > 0


def test_issue_points_with_baseline_normalisation() -> None:
    quiet = make_issue_signal(post_release_total=0, baseline_total=40, baseline_window_days=3.0)
    points, reasons, unknown = issue_points(quiet, 30.0)
    assert points == 0.0
    assert unknown is None
    assert any("baseline" in en for _zh, en in reasons)

    wave = make_issue_signal(post_release_total=90, baseline_total=10, baseline_window_days=3.0)
    points, reasons, unknown = issue_points(wave, 30.0)
    assert points >= 18.0
    assert any("baseline" in en for _zh, en in reasons)


def test_issue_points_severity_clusters() -> None:
    signal = make_issue_signal(
        post_release_total=6,
        baseline_total=6,
        baseline_window_days=3.0,
        category_counts={"data_loss": 3, "gateway_down": 1},
        open_severe_recent=1,
    )
    points, reasons, _ = issue_points(signal, 30.0)
    assert points >= 6.0
    assert any("数据损坏" in zh for zh, _en in reasons)
    assert points <= 30.0


def test_issue_points_unavailable_is_half_cap_not_zero() -> None:
    signal = IssueSignal(unavailable_reason="rate limit")
    points, _reasons, unknown = issue_points(signal, 30.0)
    assert points == pytest.approx(15.0)
    assert unknown == "rate limit"

    none_points, _r, none_unknown = issue_points(None, 30.0)
    assert none_points == pytest.approx(15.0)
    assert none_unknown is not None


def test_context_points() -> None:
    points, reasons = context_points(
        tracking_main=True, dirty_tree=True, install_kind="git", target_prerelease=False, cap=12.0
    )
    assert points > 0
    assert any("main" in r for r in reasons)
    assert points <= 12.0


def test_bonus_points_for_matured_docs_release() -> None:
    scan = scan_keywords("Documentation only release, README and UI fixes.")
    bonus, reasons = bonus_points(age_hours=800, scan=scan, signal=make_issue_signal(), total_commits=3, cap=15.0)
    assert bonus < 0
    assert reasons
    assert bonus >= -15.0


def test_classify_issues_clusters() -> None:
    issues = [
        Issue(number=1, title="[Bug] state.db corrupt after update", labels=["bug"]),
        Issue(number=2, title="bug: session lost after upgrade", labels=["bug"]),
        Issue(number=3, title="docs typo", labels=["docs"]),
    ]
    clusters = classify_issues(issues)
    assert "data_loss" in clusters
    assert len(clusters["data_loss"]) == 2


def test_gateway_cluster_needs_failure_language() -> None:
    """A mere 'gateway' mention is not a regression report."""
    benign = [
        Issue(number=1, title="gateway: add per-profile metrics"),
        Issue(number=2, title="Gateway-spawned delegate_task children leak into session_search results"),
        Issue(number=3, title="docs: how the gateway routes messages"),
    ]
    assert classify_issues(benign) == {}

    broken = [
        Issue(number=4, title="[Bug]: Gateway restart live-lock on large installs"),
        Issue(number=5, title="gateway fails to start after update"),
    ]
    clusters = classify_issues(broken)
    assert len(clusters.get("gateway_down", [])) == 2


def test_issue_points_refuses_unreliable_baseline_scale() -> None:
    """Real case: a high-traffic repo files 28k issues in 3 days - counts are useless."""
    signal = make_issue_signal(post_release_total=102, baseline_total=28132, baseline_window_days=3.0)
    points, reasons, _ = issue_points(signal, 30.0)
    assert points == 4.0
    assert any("too large" in en for _zh, en in reasons)


def test_issue_points_flags_a_giant_post_release_wave() -> None:
    signal = make_issue_signal(post_release_total=5000, baseline_total=10, baseline_window_days=3.0)
    points, reasons, _ = issue_points(signal, 30.0)
    assert points >= 16.0
    assert any("very large absolute volume" in en for _zh, en in reasons)


# --------------------------------------------------------------------------- #
# full assessments (scenario tests)
# --------------------------------------------------------------------------- #


def test_scenario_low_risk_docs_release() -> None:
    """A small documentation/UI release, 10 days old, nothing filed -> LOW / UPDATE."""
    ctx = CheckContext(
        local_version="0.21.2",
        local_tag="v2026.9.11",
        install_kind="git",
        latest_version="0.21.3",
        latest_tag="v2026.9.14",
        release_age_hours=240.0,
        release_body="Documentation and UI polish. Minor fixes in the README.",
        total_commits=6,
        pr_count=4,
        commit_subjects=["docs: clarify install steps", "ui: tweak sidebar"],
        issues=make_issue_signal(),
        weights=WEIGHTS,
    )
    result = assess(ctx)
    assert result.level == LEVEL_LOW, result.to_dict()
    assert result.recommendation == RECOMMEND_UPDATE
    assert result.stability == 100 - (result.score or 0)


def test_scenario_typical_patch_release_needs_observation() -> None:
    """Session/db changes, 3 days old, some regression reports -> not LOW, WAIT."""
    ctx = CheckContext(
        local_version="0.21.2",
        local_tag="v2026.9.11",
        install_kind="git",
        latest_version="0.21.3",
        latest_tag="v2026.9.14",
        release_age_hours=72.0,
        release_body=(
            "# Hermes Agent v0.21.3\n\nRolls up merged PRs.\n"
            "- fix(state): maintenance on state.db\n- fix(session): store sessions transactionally\n"
            "- fix(gateway): restart loop\n- config migration for tools\n"
        ),
        total_commits=338,
        pr_count=120,
        commit_subjects=["fix(state): state.db holder scan", "fix(config): migrate tools section"],
        issues=make_issue_signal(
            post_release_total=12,
            baseline_total=6,
            baseline_window_days=3.0,
            category_counts={"data_loss": 2, "gateway_down": 1},
            open_severe_recent=1,
        ),
        weights=WEIGHTS,
    )
    result = assess(ctx)
    assert result.score is not None
    assert result.level in {LEVEL_LOW_MEDIUM, LEVEL_MEDIUM, LEVEL_HIGH}
    assert result.recommendation == RECOMMEND_WAIT
    assert result.recheck_in_days and result.recheck_in_days >= 1
    # the youngest release can never be recommended even if the score were low
    assert result.summary_zh


def test_scenario_fresh_massive_rollup_is_high() -> None:
    ctx = CheckContext(
        local_version="0.21.2",
        local_tag="v2026.9.11",
        install_kind="git",
        latest_version="0.21.3",
        latest_tag="v2026.9.14",
        release_age_hours=8.0,
        release_body="Patch tag that rolls up ~1039 commits: state.db, session store, gateway, config migration, auth tokens.",
        total_commits=1039,
        pr_count=338,
        commit_subjects=["fix(state): x", "fix(session): y", "fix(gateway): z"],
        issues=make_issue_signal(
            post_release_total=40,
            baseline_total=10,
            baseline_window_days=3.0,
            category_counts={"data_loss": 4, "gateway_down": 3, "upgrade_failure": 2, "config_migration": 2},
            open_severe_recent=2,
        ),
        weights=WEIGHTS,
    )
    result = assess(ctx)
    assert result.score is not None and result.score >= 61
    assert result.level in {LEVEL_HIGH, LEVEL_VERY_HIGH}
    assert result.recommendation in {RECOMMEND_WAIT, RECOMMEND_AVOID}


def test_scenario_insufficient_data_is_unknown_not_low() -> None:
    """No release data at all must never read as 'safe'."""
    ctx = CheckContext(local_version="0.21.2", install_kind="git", issues=None, weights=WEIGHTS)
    result = assess(ctx)
    assert result.level == LEVEL_UNKNOWN
    assert result.recommendation == RECOMMEND_UNKNOWN
    assert result.score is None
    assert result.stability is None
    assert result.insufficient_data is True


def test_unknown_age_forces_unknown_level() -> None:
    ctx = CheckContext(
        local_version="0.21.2",
        latest_version="0.21.3",
        latest_tag="v2026.9.14",
        release_age_hours=None,
        release_body="fix: things",
        total_commits=3,
        issues=make_issue_signal(),
        weights=WEIGHTS,
    )
    result = assess(ctx)
    assert result.level == LEVEL_UNKNOWN
    assert "release_date" in result.unknown_areas


def test_missing_issue_data_lowers_confidence_and_flags_unknown_area() -> None:
    ctx = CheckContext(
        local_version="0.21.2",
        latest_version="0.21.3",
        latest_tag="v2026.9.14",
        release_age_hours=200.0,
        release_body="docs: cleanup",
        total_commits=4,
        issues=IssueSignal(unavailable_reason="HTTP 403 rate limit"),
        weights=WEIGHTS,
    )
    result = assess(ctx)
    assert "github_issues" in result.unknown_areas
    issues_factor = next(f for f in result.factors if f.key == "issues")
    assert issues_factor.unknown is True
    assert issues_factor.points == pytest.approx(15.0)
    assert result.confidence < 0.85


def test_score_is_capped_at_100_and_stability_mirrors_it() -> None:
    ctx = CheckContext(
        local_version="0.21.1",
        local_tag="v2026.9.7",
        tracking_main=True,
        install_kind="unknown",
        dirty_tree=True,
        latest_version="1.0.0",
        latest_tag="v2026.9.14",
        latest_prerelease=True,
        release_age_hours=2.0,
        release_body="BREAKING CHANGE: full rewrite. database migration, state.db, sessions, auth tokens, gateway, config, storage backend, provider sdk api mcp memory.",
        total_commits=2500,
        pr_count=900,
        issues=make_issue_signal(
            post_release_total=200,
            baseline_total=5,
            baseline_window_days=3.0,
            category_counts={"data_loss": 9, "gateway_down": 9, "upgrade_failure": 9, "config_migration": 9},
            open_severe_recent=5,
        ),
        weights=WEIGHTS,
    )
    result = assess(ctx)
    assert result.score == 100
    assert result.level == LEVEL_VERY_HIGH
    assert result.stability == 0
    assert result.recommendation == RECOMMEND_AVOID


def test_level_bands() -> None:
    assert level_for_score(0) == LEVEL_LOW
    assert level_for_score(20) == LEVEL_LOW
    assert level_for_score(21) == LEVEL_LOW_MEDIUM
    assert level_for_score(40) == LEVEL_LOW_MEDIUM
    assert level_for_score(41) == LEVEL_MEDIUM
    assert level_for_score(60) == LEVEL_MEDIUM
    assert level_for_score(61) == LEVEL_HIGH
    assert level_for_score(80) == LEVEL_HIGH
    assert level_for_score(81) == LEVEL_VERY_HIGH
    assert level_for_score(100) == LEVEL_VERY_HIGH
    assert level_for_score(None) == LEVEL_UNKNOWN


def test_threshold_and_minimum_age_gate() -> None:
    """Score below the threshold but a very young release -> WAIT until it matures."""
    ctx = CheckContext(
        local_version="0.21.2",
        latest_version="0.21.3",
        latest_tag="v2026.9.14",
        release_age_hours=12.0,
        release_body="docs: small fix",
        total_commits=2,
        issues=make_issue_signal(),
        risk_threshold=60,
        minimum_release_age_days=5.0,
        weights=WEIGHTS,
    )
    result = assess(ctx)
    assert result.score is not None and result.score <= 60
    assert result.recommendation == RECOMMEND_WAIT
    assert result.recheck_in_days == 5


# --------------------------------------------------------------------------- #
# phase 2: missing evidence must never look safer
# --------------------------------------------------------------------------- #


def _ctx_with_issues(signal) -> CheckContext:  # noqa: ANN001
    return CheckContext(
        local_version="0.21.2",
        latest_version="0.21.3",
        latest_tag="v2026.9.14",
        release_age_hours=100.0,
        release_body="fix(state): session store\n" * 8,
        total_commits=40,
        issues=signal,
        weights=WEIGHTS,
    )


def test_missing_issue_data_never_makes_a_release_look_safer() -> None:
    """The core rule: missing evidence != zero risk."""
    observed_clean = assess(_ctx_with_issues(make_issue_signal()))
    missing = assess(_ctx_with_issues(IssueSignal(unavailable_reason="HTTP 403 rate limit")))

    assert missing.score is not None and observed_clean.score is not None
    assert missing.score >= observed_clean.score
    assert missing.regression_signal is None
    assert missing.score_is_lower_bound is True
    assert missing.regression_detail is not None
    assert missing.regression_detail.floor == UNKNOWN_REGRESSION_FLOOR
    assert observed_clean.score_is_lower_bound is False


def test_observed_regression_is_folded_into_the_regression_signal() -> None:
    from hermes_update_check.clusters import build_clusters
    from hermes_update_check.github_api import Issue

    issues = [
        Issue(number=1, title="[Bug] state.db corrupt after update", author="alice", state="open"),
        Issue(number=2, title="[Bug] state.db corrupt, data loss", author="bob", state="open"),
        Issue(number=3, title="[Bug] session lost after upgrade", author="carol", state="open"),
    ]
    signal = make_issue_signal(post_release_total=12, baseline_total=6)
    signal.clusters = build_clusters(issues, release_version="0.21.3")

    result = assess(_ctx_with_issues(signal))
    assert result.regression_signal is not None and result.regression_signal > 30
    assert any("DATABASE" in item or "数据库" in item for item in result.summary_zh + result.summary_en)
    # overall is never below the strongest component
    assert result.overall is not None and result.overall >= (result.regression_signal or 0) - 1


def test_ignore_hard_gates_config_is_not_needed_but_flags_exist() -> None:
    """A LOW-risk release with missing issue data is still flagged as incomplete."""
    missing = assess(_ctx_with_issues(IssueSignal(unavailable_reason="rate limit")))
    assert missing.score_is_lower_bound is True
    factor = next(f for f in missing.factors if f.key == "regression_clusters")
    assert factor.unknown is True


def test_lower_bound_display_at_the_ceiling() -> None:
    """`>= 100` is nonsense; at the ceiling show '100 (lower bound)'."""
    assessment = RiskAssessment(
        score=100, level="VERY HIGH", stability=0, recommendation=RECOMMEND_AVOID, confidence=0.8, score_is_lower_bound=True
    )
    assert assessment.score_or_unknown == "100 (lower bound)"
    assessment.score_is_lower_bound = False
    assert assessment.score_or_unknown == "100/100"


def test_factors_always_explain_themselves() -> None:
    ctx = CheckContext(
        local_version="0.21.2",
        latest_version="0.21.3",
        latest_tag="v2026.9.14",
        release_age_hours=100.0,
        release_body="fix(state): session store",
        total_commits=40,
        issues=make_issue_signal(),
        weights=WEIGHTS,
    )
    result = assess(ctx)
    keys = {f.key for f in result.factors}
    # the graded regression-cluster factor joined the original six
    assert keys == {"keywords", "age", "volume", "issues", "regression_clusters", "context", "bonus"}
    for factor in result.factors:
        assert factor.label_zh and factor.label_en
        assert factor.detail_zh and factor.detail_en


def test_component_view_is_populated() -> None:
    """Change Risk / Regression Signal / Data Confidence / Overall are all reported."""
    ctx = CheckContext(
        local_version="0.21.2",
        latest_version="0.21.3",
        latest_tag="v2026.9.14",
        release_age_hours=100.0,
        release_body="fix(state): session store migration\n" * 12,
        total_commits=40,
        issues=make_issue_signal(),
        provenance_ok=True,
        weights=WEIGHTS,
    )
    result = assess(ctx)
    assert result.change_risk is not None and 0 <= result.change_risk <= 100
    assert result.regression_signal is not None and 0 <= result.regression_signal <= 100
    assert result.data_confidence is not None and 0 <= result.data_confidence <= 100
    assert result.overall == result.score
    rows = dict(result.component_rows())
    assert "Change Risk" in rows and "Regression Signal" in rows
    assert "Data Confidence" in rows and "Overall Risk" in rows

    # multiplicative combination: overall is never below the strongest component
    assert result.overall >= result.change_risk
    assert result.overall >= result.regression_signal
