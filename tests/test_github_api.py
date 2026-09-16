"""GitHub payload parsing tests (fixtures only - no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hermes_update_check.github_api import (
    MAX_QUERY_LENGTH,
    CommitInfo,
    CompareResult,
    GitHubClient,
    build_issue_query,
    extract_display_version,
    extract_pr_count,
    parse_repo_slug,
)
from hermes_update_check.http import HttpClient

RELEASE_FIXTURE = {
    "tag_name": "v2026.9.14",
    "name": "Hermes Agent v0.21.3 (v2026.9.14)",
    "body": "# Hermes Agent v0.21.3 (v2026.9.14)\n\nPatch release that rolls up ~338 PRs.",
    "html_url": "https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.14",
    "published_at": "2026-09-14T16:04:14Z",
    "created_at": "2026-09-14T16:04:09Z",
    "prerelease": False,
    "draft": False,
    "author": {"login": "teknium1"},
}

COMPARE_FIXTURE = {
    "status": "ahead",
    "ahead_by": 1039,
    "behind_by": 0,
    "total_commits": 1039,
    "html_url": "https://github.com/NousResearch/hermes-agent/compare/v2026.9.11...v2026.9.14",
    "files": [{"filename": "a.py"}],
    "commits": [
        {
            "sha": "e609efb0" + "0" * 32,
            "commit": {
                "message": "fix(mcp): shared connection trust policy (#1234)",
                "author": {"date": "2026-09-13T10:00:00Z"},
            },
        },
        {
            "sha": "12173db5" + "0" * 32,
            "commit": {
                "message": "fix(state): maintenance on state.db refuses foreign holder (#1240)",
                "author": {"date": "2026-09-14T10:00:00Z"},
            },
        },
    ],
}

ISSUE_SEARCH_FIXTURE = {
    "total_count": 2,
    "items": [
        {
            "number": 111604,
            "title": "[Bug]: state.db corrupt after update",
            "state": "open",
            "created_at": "2026-09-15T06:16:16Z",
            "html_url": "https://github.com/NousResearch/hermes-agent/issues/111604",
            "labels": [{"name": "bug"}, {"name": "state"}, "docs"],
            "comments": 3,
        },
        {
            "number": 111598,
            "title": "gateway fails to start",
            "state": "closed",
            "created_at": "2026-09-15T06:09:27Z",
            "html_url": "https://github.com/NousResearch/hermes-agent/issues/111598",
            "labels": [],
            "comments": 0,
        },
    ],
}


class StubHttp(HttpClient):
    """HttpClient that answers from a fixture map instead of the network."""

    def __init__(self, routes):
        super().__init__(timeout=1, retries=0, cache=None)
        self.routes = routes
        self.calls: list[str] = []

    def get_json(self, url, *, params=None, extra_headers=None, use_cache=True, cache_key=None):
        from hermes_update_check.http import HttpResult

        self.calls.append(url)
        for key, payload in self.routes.items():
            if key in url:
                return HttpResult(url=url, status=200, data=payload)
        return HttpResult(url=url, status=404, error="HTTP 404: not found")


def make_client(routes) -> GitHubClient:
    return GitHubClient(repo="NousResearch/hermes-agent", http=StubHttp(routes), max_issue_searches=5)


def test_extract_display_version_skips_the_date_tag() -> None:
    assert extract_display_version("Hermes Agent v0.21.3 (v2026.9.14)", "", "v2026.9.14") == "0.21.3"
    assert extract_display_version("Hermes Agent 0.21.0", "# v0.21.0\n", "v2026.8.31") == "0.21.0"
    # semver-only repositories still work
    assert extract_display_version("", "", "v1.2.3") == "1.2.3"


def test_list_and_pick_latest_release() -> None:
    client = make_client({"/releases": [RELEASE_FIXTURE]})
    releases = client.list_releases()
    assert len(releases) == 1
    release = releases[0]
    assert release.tag == "v2026.9.14"
    assert release.display_version == "0.21.3"
    assert release.prerelease is False
    assert release.when is not None and release.when.year == 2026
    latest = client.latest_release()
    assert latest is not None and latest.tag == release.tag


def test_prereleases_are_skipped_unless_requested() -> None:
    pre = dict(RELEASE_FIXTURE, tag_name="v2026.9.20", name="Hermes Agent v0.22.0-rc1", prerelease=True)
    client = make_client({"/releases": [pre, RELEASE_FIXTURE]})
    assert client.latest_release().tag == "v2026.9.14"
    assert client.latest_release(include_prereleases=True).tag == "v2026.9.20"


def test_compare_parsing_and_truncation() -> None:
    client = make_client({"/compare/": COMPARE_FIXTURE})
    compare = client.compare("v2026.9.11", "v2026.9.14")
    assert isinstance(compare, CompareResult)
    assert compare.total_commits == 1039
    assert len(compare.commits) == 2
    assert compare.truncated is True  # 2 commits in payload vs 1039 total
    assert compare.pr_numbers == [1234, 1240]
    assert "state.db" in compare.commit_corpus
    assert compare.to_dict()["truncated"] is True


def test_compare_failure_degrades_gracefully() -> None:
    client = make_client({})
    assert client.compare("v1", "v2") is None
    assert client.degradations  # recorded, not raised


def test_issue_search_parsing() -> None:
    client = make_client({"/search/issues": ISSUE_SEARCH_FIXTURE})
    result = client.search_issues("repo:x type:issue")
    assert result.ok is True
    assert result.total_count == 2
    first = result.items[0]
    assert first.number == 111604
    assert first.labels == ["bug", "state", "docs"]
    assert first.created_at is not None and first.created_at.tzinfo is not None
    assert result.items[1].state == "closed"


def test_issue_search_budget_is_enforced() -> None:
    client = GitHubClient(repo="x/y", http=StubHttp({"/search/issues": ISSUE_SEARCH_FIXTURE}), max_issue_searches=1)
    assert client.search_issues("a").ok is True
    second = client.search_issues("b")
    assert second.ok is False
    assert "budget" in (second.error or "")


def test_build_issue_query_is_date_only_and_bounded() -> None:
    after = datetime(2026, 9, 14, 16, 4, 14, tzinfo=timezone.utc)
    query = build_issue_query("NousResearch/hermes-agent", created_after=after, keywords=("bug", "crash", "state.db"))
    assert "created:>=2026-09-14" in query
    assert "T16:04:14" not in query
    assert "in:title" in query
    assert " OR " in query
    assert len(query) <= MAX_QUERY_LENGTH

    long_keywords = tuple(f"keyword-{i}" for i in range(60))
    long_query = build_issue_query("a/b", created_after=after, keywords=long_keywords)
    assert len(long_query) <= MAX_QUERY_LENGTH

    labelled = build_issue_query("a/b", created_after=after, label="bug")
    assert 'label:"bug"' in labelled
    windowed = build_issue_query("a/b", created_after=after, created_before=after + timedelta(days=3))
    assert "created:<" in windowed


def test_build_issue_query_refuses_more_than_five_operators() -> None:
    """GitHub returns 422 'More than five AND / OR / NOT operators were used'."""
    after = datetime(2026, 9, 14, tzinfo=timezone.utc)
    many = tuple(f"kw{i}" for i in range(12))
    query = build_issue_query("a/b", created_after=after, keywords=many)
    assert query.count(" OR ") <= 5  # five operators == six terms
    assert len(query) <= MAX_QUERY_LENGTH


def test_extract_pr_count_from_release_notes() -> None:
    assert extract_pr_count("This tag rolls up the ~338 PRs merged since v0.21.2.") == 338
    assert extract_pr_count("Bundles 42 pull requests from the last week.") == 42
    assert extract_pr_count("no numbers here") is None
    assert extract_pr_count(None) is None
    assert extract_pr_count("nothing suspicious about 99 bottles") is None


def test_parse_repo_slug() -> None:
    assert parse_repo_slug("NousResearch/hermes-agent") == ("NousResearch", "hermes-agent")
    with pytest.raises(ValueError):
        parse_repo_slug("broken")


def test_commit_pr_number_variants() -> None:
    assert CommitInfo(sha="x", message="fix: thing (#42)").pr_number == 42
    assert CommitInfo(sha="x", message="Merge pull request #77 from feature").pr_number == 77
    assert CommitInfo(sha="x", message="docs: no pr here").pr_number is None
