"""Orchestration tests: run_check with an injected (fake) GitHub client."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeGitHubClient, make_compare, make_issue, make_release

from hermes_update_check.checker import (
    ISSUE_KEYWORDS,
    collect_issue_signal,
    run_check,
)
from hermes_update_check.local_env import GitState, LocalEnv
from hermes_update_check.risk import RECOMMEND_UPDATE, RECOMMEND_WAIT


def make_env(
    hermes_home: Path,
    *,
    version: str = "0.21.2",
    tag: str | None = "v2026.9.11",
    branch: str = "main",
    dirty: bool = False,
    tags_at_head: list[str] | None = None,
    install_kind: str = "git",
) -> LocalEnv:
    return LocalEnv(
        hermes_home=hermes_home,
        install_kind=install_kind,
        install_dir=hermes_home / "hermes-agent",
        hermes_cli=None,
        version=version,
        release_tag=tag,
        commit="5eb99eb2",
        python_version="3.11.16",
        git=GitState(
            is_repo=True,
            branch=branch,
            commit="5eb99eb2",
            full_commit="5eb99eb2" + "0" * 32,
            dirty=dirty,
            dirty_files=["website/docs/x.mdx"] if dirty else [],
            tags_at_head=list(tags_at_head or []),
        ),
    )


def test_run_check_full_picture(cfg, hermes_home, state_root) -> None:
    releases = [
        make_release(tag="v2026.9.14", version="0.21.3", age_hours=30, body="fix(state): state.db maintenance"),
        make_release(tag="v2026.9.11", version="0.21.2", age_hours=100),
        make_release(tag="v2026.9.7", version="0.21.1", age_hours=200),
    ]
    client = FakeGitHubClient(
        releases=releases,
        compare=make_compare(commits=40, subjects=["fix(state): state.db", "fix(gateway): restart"]),
        search={
            "created:<": (3, []),
            'label:"bug"': (2, [make_issue(1, "[Bug]: gateway fails to start")]),
            "in:title": (5, [make_issue(2, "[Bug]: state.db corrupt after update"), make_issue(3, "crash on startup")]),
        },
    )
    env = make_env(hermes_home, tags_at_head=["v2026.9.11"])  # a plain stable install
    check = run_check(cfg, env=env, client=client, state_root=state_root)

    assert check.latest is not None and check.latest.tag == "v2026.9.14"
    assert check.previous is not None and check.previous.tag == "v2026.9.11"
    assert check.target_version == "0.21.3"
    assert check.update_available is True
    assert check.update_status == "update_available"
    assert check.channel == "STABLE"
    assert check.versions_behind == 1
    assert check.compare is not None and check.compare.total_commits == 40
    assert check.issues is not None and check.issues.available
    assert check.issues.post_release_total == 5
    assert check.assessment is not None
    assert check.assessment.factors
    # phase 2: provenance, gates and the advisor verdict all ran
    assert check.provenance is not None and check.provenance.tag_matched is True
    assert check.gates is not None and check.gates.enabled
    assert check.recommendation is not None
    # the verdict must be actionable, i.e. not silently "update now"
    assert check.action in {RECOMMEND_UPDATE, RECOMMEND_WAIT}


def test_run_check_up_to_date(cfg, hermes_home, state_root) -> None:
    releases = [make_release(tag="v2026.9.11", version="0.21.2", age_hours=100)]
    client = FakeGitHubClient(releases=releases, compare=make_compare())
    env = make_env(hermes_home, tags_at_head=["v2026.9.11"])
    check = run_check(cfg, env=env, client=client, state_root=state_root)
    assert check.update_available is False
    assert check.update_status == "up_to_date"
    assert check.recommended_action == "UP_TO_DATE"


def test_run_check_without_release_data_is_insufficient(cfg, hermes_home, state_root) -> None:
    client = FakeGitHubClient(releases=[])
    check = run_check(cfg, env=make_env(hermes_home), client=client, state_root=state_root)
    assert check.latest is None
    assert check.assessment is not None
    assert check.assessment.insufficient_data is True
    assert check.assessment.recommendation == "INSUFFICIENT_DATA"
    assert check.degradation


def test_run_check_skips_prereleases_by_default(cfg, hermes_home, state_root) -> None:
    releases = [
        make_release(tag="v2026.9.20", version="0.22.0", prerelease=True, age_hours=5),
        make_release(tag="v2026.9.14", version="0.21.3", age_hours=40),
    ]
    client = FakeGitHubClient(releases=releases, compare=make_compare())
    check = run_check(cfg, env=make_env(hermes_home), client=client, state_root=state_root)
    assert check.latest.tag == "v2026.9.14"

    cfg.allow_prerelease = True
    check = run_check(cfg, env=make_env(hermes_home), client=client, state_root=state_root)
    assert check.latest.tag == "v2026.9.20"
    assert check.assessment is not None
    # a prerelease target raises the context factor
    context = next(f for f in check.assessment.factors if f.key == "context")
    assert context.points > 0


def test_run_check_tracks_main_ahead(cfg, hermes_home, state_root) -> None:
    releases = [make_release(tag="v2026.9.14", version="0.21.3", age_hours=30)]
    client = FakeGitHubClient(
        releases=releases,
        compare=make_compare(commits=120),
        head_compare=make_compare(base="v2026.9.14", head="main", commits=120),
    )
    check = run_check(
        cfg, env=make_env(hermes_home, version="0.21.2", tag="v2026.9.11"), client=client, state_root=state_root
    )
    assert check.main_ahead_commits == 120
    assert check.channel == "MAIN"
    assert check.update_status == "ahead_of_stable"
    assert check.update_available is False
    assert check.action == "AHEAD_OF_STABLE"
    assert check.assessment is not None
    assert any("main" in item for item in check.assessment.summary_zh)


def test_run_check_uses_release_notes_pr_count_when_compare_is_truncated(cfg, hermes_home, state_root) -> None:
    body = "Patch release. This tag rolls up the ~338 PRs merged since v0.21.2 into a stable tagged release."
    releases = [make_release(tag="v2026.9.14", version="0.21.3", age_hours=30, body=body)]
    client = FakeGitHubClient(releases=releases, compare=make_compare(commits=1039, truncated=True))
    check = run_check(cfg, env=make_env(hermes_home), client=client, state_root=state_root)
    assert check.pr_count == 338
    assert check.pr_count_from_notes is True
    assert check.assessment is not None
    volume = next(f for f in check.assessment.factors if f.key == "volume")
    assert volume.points > 0


def test_run_check_respects_issue_opt_out(cfg, hermes_home, state_root) -> None:
    releases = [make_release(tag="v2026.9.14", version="0.21.3", age_hours=30)]
    client = FakeGitHubClient(releases=releases, compare=make_compare())
    check = run_check(cfg, env=make_env(hermes_home), client=client, state_root=state_root, include_issues=False)
    assert check.issues is None
    assert check.assessment is not None
    issues_factor = next(f for f in check.assessment.factors if f.key == "issues")
    assert issues_factor.points == pytest.approx(15.0)


def test_fresh_cache_hit_does_not_degrade_the_issue_signal(cfg, state_root) -> None:
    """Regression (found in the real E2E): a *fresh* cache hit was marked degraded.

    Fresh cache is normal operation; only a stale fallback (network failed) is
    degraded data.
    """
    from hermes_update_check.github_api import IssueSearchResult

    class FreshCacheClient:
        degradations: list[str] = []

        def search_issues(self, query, **kwargs):
            return IssueSearchResult(query=query, total_count=3, items=[], ok=True, degraded=False, from_cache=True)

        def get_issue_comments(self, number, **kwargs):
            return []

    signal = collect_issue_signal(cfg, FreshCacheClient(), make_release(age_hours=10))
    assert signal.available is True
    assert signal.degraded is False
    assert signal.signal_confidence is not None and signal.signal_confidence >= 70


def test_stale_cache_marks_the_issue_signal_degraded(cfg) -> None:
    from hermes_update_check.github_api import IssueSearchResult

    class StaleCacheClient:
        degradations: list[str] = []

        def search_issues(self, query, **kwargs):
            return IssueSearchResult(query=query, total_count=3, items=[], ok=True, degraded=True, from_cache=True)

        def get_issue_comments(self, number, **kwargs):
            return []

    signal = collect_issue_signal(cfg, StaleCacheClient(), make_release(age_hours=10))
    assert signal.degraded is True


def test_comment_enrichment_marks_maintainer_confirmation(cfg, state_root) -> None:
    """A maintainer reply with confirmation language is picked up."""
    from hermes_update_check.github_api import IssueComment

    clusters_issues = [
        make_issue(101, "[Bug] state.db corrupt after update", author="alice"),
        make_issue(102, "[Bug] state.db corrupt, data loss", author="bob"),
    ]
    client = FakeGitHubClient(
        search={"created:<": (2, []), 'label:"bug"': (2, clusters_issues), "in:title": (2, clusters_issues)},
        comments={
            101: [
                IssueComment(
                    author="teknium1", body="Confirmed, reproducing locally - will fix.", author_association="OWNER"
                )
            ]
        },
    )
    signal = collect_issue_signal(cfg, client, make_release(age_hours=30))
    assert signal.enriched_issues >= 1
    database = next(c for c in signal.clusters if c.key == "DATABASE")
    assert database.maintainer_confirmed >= 1
    assert database.confidence in {"MEDIUM", "HIGH"}


def test_collect_issue_signal_clusters(cfg, state_root) -> None:
    release = make_release(tag="v2026.9.14", version="0.21.3", age_hours=72)
    items = [
        make_issue(1, "[Bug]: state.db corrupt after update"),
        make_issue(2, "[Bug]: database locked, session lost"),
        make_issue(3, "[Bug]: gateway won't start"),
        make_issue(4, "docs: typo"),
    ]
    client = FakeGitHubClient(
        search={"created:<": (4, []), 'label:"bug"': (4, items), "in:title": (9, items)},
    )
    signal = collect_issue_signal(cfg, client, release)
    assert signal.available
    assert signal.post_release_total == 9
    assert signal.baseline_total == 4
    assert signal.category_counts.get("data_loss", 0) >= 1
    assert signal.category_counts.get("gateway_down", 0) >= 1
    assert signal.scanned_items == 4
    assert signal.window_days == pytest.approx(3.0, rel=0.1)
    assert ISSUE_KEYWORDS[0] in signal.queries[0]


def test_collect_issue_signal_without_timestamp(cfg) -> None:
    release = make_release(age_hours=1)
    release.published_at = None
    release.created_at = None
    client = FakeGitHubClient()
    signal = collect_issue_signal(cfg, client, release)
    assert signal.available is False
    assert "timestamp" in (signal.unavailable_reason or "")


def test_collect_issue_signal_marks_failure(cfg) -> None:
    from hermes_update_check.github_api import IssueSearchResult

    class Failing:
        degradations: list[str] = []

        def search_issues(self, query, **kwargs):
            return IssueSearchResult(query=query, ok=False, degraded=True, error="HTTP 403 rate limit")

    signal = collect_issue_signal(cfg, Failing(), make_release(age_hours=10))
    assert signal.available is False
    assert "rate limit" in (signal.unavailable_reason or "")
