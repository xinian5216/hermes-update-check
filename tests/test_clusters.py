"""Regression-cluster tests: grading, independence and credibility."""

from __future__ import annotations

from hermes_update_check.clusters import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    UNKNOWN_REGRESSION_FLOOR,
    IssueEnrichment,
    build_clusters,
    cluster_keys_for,
    grade_confidence,
    regression_signal,
    severity_for,
)
from hermes_update_check.github_api import Issue


def issue(
    number: int,
    title: str,
    *,
    author: str = "alice",
    state: str = "open",
    body: str = "",
    labels=None,
    comments: int = 0,
) -> Issue:
    return Issue(
        number=number,
        title=title,
        state=state,
        author=author,
        body=body,
        labels=list(labels or ["bug"]),
        comments=comments,
    )


# --------------------------------------------------------------------------- #
# classification and severity
# --------------------------------------------------------------------------- #


def test_cluster_classification_covers_the_nine_classes() -> None:
    cases = {
        "DATABASE": "[Bug] state.db corrupt after update",
        "SESSION": "[Bug] session lost after upgrade",
        "GATEWAY": "[Bug] gateway fails to start",
        "CONFIG_MIGRATION": "config migration broke config.yaml during update",
        "UPDATE_FAILURE": "hermes update fails and rollback also fails",
        "AUTH": "auth token rejected after update (401)",
        "CRASH": "[Bug] segfault in the TUI",
        "MCP": "mcp server fails to start after update",
        "PROVIDER": "provider broken: model requests fail",
    }
    for expected, title in cases.items():
        keys = cluster_keys_for(issue(1, title))
        assert expected in keys, f"{title!r} -> {keys}"


def test_benign_titles_do_not_cluster() -> None:
    for title in (
        "gateway: add per-profile metrics",
        "docs: how the session store works",
        "config: new option for the theme",
        "[Feature] support provider X",
    ):
        assert cluster_keys_for(issue(1, title)) == [], title


def test_severity_promotion_to_critical() -> None:
    assert severity_for("DATABASE", "state.db corrupt, data loss") == SEVERITY_CRITICAL
    assert severity_for("DATABASE", "state.db locked occasionally") == SEVERITY_HIGH
    assert severity_for("SESSION", "session lost after upgrade") == SEVERITY_CRITICAL
    assert severity_for("SESSION", "session resume is slow") == SEVERITY_HIGH
    assert severity_for("PROVIDER", "one provider model fails") == SEVERITY_MEDIUM


# --------------------------------------------------------------------------- #
# independence / credibility
# --------------------------------------------------------------------------- #


def test_same_author_three_issues_is_one_reporter() -> None:
    issues = [issue(i, "[Bug] state.db corrupt after update", author="alice") for i in (1, 2, 3)]
    clusters = build_clusters(issues)
    database = next(c for c in clusters if c.key == "DATABASE")
    assert database.reports == 3
    assert database.unique_reporters == 1
    assert database.confidence == CONFIDENCE_LOW  # one voice, even with three reports
    assert any("同一位报告人" in note for note in database.notes_zh)


def test_multiple_independent_reporters_raise_confidence() -> None:
    issues = [
        issue(1, "[Bug] gateway fails to start", author="alice"),
        issue(2, "[Bug] gateway crash loop on startup", author="bob"),
        issue(3, "[Bug] gateway won't start after update", author="carol"),
    ]
    clusters = build_clusters(issues)
    gateway = next(c for c in clusters if c.key == "GATEWAY")
    assert gateway.unique_reporters == 3
    assert gateway.severity in {SEVERITY_HIGH, SEVERITY_CRITICAL}
    assert gateway.confidence in {CONFIDENCE_MEDIUM, CONFIDENCE_HIGH}
    assert gateway.contribution > 0.4


def test_duplicate_reports_are_not_independent() -> None:
    """Two identical titles from the same author must not read as a wave."""
    issues = [issue(i, "[Bug] state.db corrupt after update", author="alice", body="same report") for i in (1, 2)]
    clusters = build_clusters(issues)
    database = next(c for c in clusters if c.key == "DATABASE")
    assert database.unique_reporters == 1
    assert database.confidence == CONFIDENCE_LOW


def test_maintainer_confirmation_and_linked_pr_raise_confidence() -> None:
    issues = [
        issue(1, "[Bug] session lost after upgrade", author="alice"),
        issue(2, "[Bug] session data lost on restart", author="bob"),
    ]
    enrichment = {
        1: IssueEnrichment(number=1, maintainer_reply=True, maintainer_confirmed=True, linked_pr=True, comments=4),
        2: IssueEnrichment(number=2, maintainer_reply=True, comments=2),
    }
    clusters = build_clusters(issues, enrichment=enrichment)
    session = next(c for c in clusters if c.key == "SESSION")
    assert session.maintainer_confirmed == 1
    assert session.linked_pr == 1
    assert session.confidence in {CONFIDENCE_MEDIUM, CONFIDENCE_HIGH}
    assert any("maintainer" in note for note in session.notes_en)


def test_version_mention_and_reproduction_count_towards_confidence() -> None:
    issues = [
        issue(
            1,
            "[Bug] config migration broke my config after 0.21.3",
            author="alice",
            body="Steps to reproduce:\n1. run hermes update",
        ),
        issue(2, "[Bug] config.yaml invalid after updating to v0.21.3", author="bob", body="error: KeyError 'mcp'"),
    ]
    clusters = build_clusters(issues, release_version="0.21.3", release_tag="v2026.9.14")
    config = next(c for c in clusters if c.key == "CONFIG_MIGRATION")
    assert config.mentions_version == 2
    assert config.with_reproduction >= 1
    assert config.confidence in {CONFIDENCE_MEDIUM, CONFIDENCE_HIGH}


def test_grade_confidence_weights() -> None:
    low = grade_confidence(
        reports=1,
        unique_reporters=1,
        open_count=0,
        maintainer_confirmed=0,
        linked_pr=0,
        with_reproduction=0,
        mentions_version=0,
        signature_groups=1,
    )
    high = grade_confidence(
        reports=3,
        unique_reporters=3,
        open_count=3,
        maintainer_confirmed=1,
        linked_pr=1,
        with_reproduction=1,
        mentions_version=1,
        signature_groups=2,
    )
    assert low == CONFIDENCE_LOW
    assert high == CONFIDENCE_HIGH


def test_clusters_are_sorted_by_severity_then_contribution() -> None:
    issues = [
        issue(1, "[Bug] state.db corrupt", author="a"),
        issue(2, "[Bug] state.db corrupt", author="b"),
        issue(3, "provider fails for one model", author="c"),
    ]
    clusters = build_clusters(issues)
    assert clusters[0].severity == SEVERITY_CRITICAL
    assert clusters[-1].severity == SEVERITY_MEDIUM


# --------------------------------------------------------------------------- #
# regression signal
# --------------------------------------------------------------------------- #


def test_regression_signal_grows_with_severity_and_reporters() -> None:
    single = build_clusters([issue(1, "[Bug] gateway fails to start", author="alice")])
    multi = build_clusters(
        [
            issue(1, "[Bug] gateway fails to start", author="alice"),
            issue(2, "[Bug] gateway crash loop", author="bob", state="closed"),
        ]
    )
    single_signal = regression_signal(single)
    multi_signal = regression_signal(multi)
    assert single_signal.value is not None and multi_signal.value is not None
    assert multi_signal.value > single_signal.value


def test_regression_signal_unavailable_is_a_floor_not_zero() -> None:
    signal = regression_signal([], unavailable=True, unavailable_reason="HTTP 403 rate limit")
    assert signal.value is None
    assert signal.floor == UNKNOWN_REGRESSION_FLOOR
    assert signal.unknown is True
    assert "rate limit" in signal.reasons_en[0]
    assert signal.display.startswith("UNKNOWN")


def test_regression_signal_volume_component_is_half_weight() -> None:
    clusters = build_clusters([issue(1, "[Bug] gateway fails to start", author="alice")])
    without_volume = regression_signal(clusters, volume_ratio=0.0)
    with_volume = regression_signal(clusters, volume_ratio=1.0)
    assert with_volume.value >= without_volume.value
    # half weight, not full: the jump is bounded
    assert (with_volume.value - without_volume.value) <= 50


def test_regression_signal_does_not_saturate_with_many_clusters() -> None:
    """Regression (found in the real E2E): a noisy repo produced a dozen clusters.

    Nine clusters must not pin the signal at 100 just for existing.
    """
    issues = []
    number = 1
    for titles in (
        ["[Bug] gateway fails to start", "[Bug] gateway crash loop", "[Bug] gateway won't start"],
        ["[Bug] provider broken: model fails", "[Bug] provider error on request"],
        ["[Bug] segfault in TUI", "[Bug] crash on startup"],
        ["[Bug] state.db corrupt", "[Bug] state.db locked"],
        ["[Bug] session lost", "[Bug] auth token rejected"],
        ["[Bug] config migration broke config.yaml", "[Bug] hermes update fails"],
        ["[Bug] mcp server fails to start"],
    ):
        for title in titles:
            issues.append(issue(number, title, author=f"user{number}"))
            number += 1
    clusters = build_clusters(issues)
    assert len(clusters) >= 7
    signal = regression_signal(clusters, signal_confidence=70)
    assert signal.value is not None
    assert signal.value < 95, f"signal saturated at {signal.value} despite damping"


def test_regression_signal_is_graded_by_issue_confidence() -> None:
    clusters = build_clusters(
        [
            issue(1, "[Bug] gateway fails to start", author="a"),
            issue(2, "[Bug] gateway crash loop", author="b"),
        ]
    )
    confident = regression_signal(clusters, signal_confidence=100)
    unsure = regression_signal(clusters, signal_confidence=30)
    assert confident.value >= unsure.value
    assert unsure.value > 0


def test_cluster_to_dict_is_json_safe() -> None:
    clusters = build_clusters([issue(1, "[Bug] session lost", author="alice", body="x" * 100)])
    data = clusters[0].to_dict()
    assert data["reports"] == 1
    assert isinstance(data["samples"], list)
    assert data["samples"][0]["author"] == "alice"
    assert data["severity"] in {SEVERITY_CRITICAL, SEVERITY_HIGH}
