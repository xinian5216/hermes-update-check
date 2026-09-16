"""Shared fixtures: hermetic, offline, no network access anywhere."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from hermes_update_check.config import Config
from hermes_update_check.github_api import CommitInfo, CompareResult, Issue, Release
from hermes_update_check.util import utcnow


@pytest.fixture()
def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes"
    (home / "logs").mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("display:\n  skin: default\ntools:\n  enabled: true\n", encoding="utf-8")
    return home


@pytest.fixture()
def cfg(hermes_home: Path, state_root: Path) -> Config:
    config = Config()
    config.paths.hermes_home = hermes_home
    config.paths.state_dir = state_root
    config.language = "zh"
    return config


def make_release(
    *,
    tag: str = "v2026.9.14",
    version: str = "0.21.3",
    age_hours: float = 30.0,
    prerelease: bool = False,
    body: str = "Patch release with bug fixes.",
    name: str | None = None,
) -> Release:
    published = utcnow() - timedelta(hours=age_hours)
    return Release(
        tag=tag,
        name=name or f"Hermes Agent v{version} ({tag})",
        body=body,
        html_url=f"https://github.com/NousResearch/hermes-agent/releases/tag/{tag}",
        published_at=published,
        created_at=published,
        prerelease=prerelease,
        display_version=version,
    )


def make_compare(
    *,
    base: str = "v2026.9.11",
    head: str = "v2026.9.14",
    commits: int = 12,
    subjects: list[str] | None = None,
    truncated: bool = False,
) -> CompareResult:
    subjects = subjects or [f"fix(ui): small tweak {i}" for i in range(min(commits, 5))]
    payload = [
        CommitInfo(sha=f"{i:040x}", message=subject, date=utcnow() - timedelta(days=1))
        for i, subject in enumerate(subjects)
    ]
    return CompareResult(
        base_tag=base,
        head_tag=head,
        status="ahead",
        total_commits=commits,
        ahead_by=commits,
        commits=payload,
        files_changed=10,
        html_url="https://github.com/NousResearch/hermes-agent/compare/x...y",
    )


def make_issue(
    number: int,
    title: str,
    *,
    state: str = "open",
    age_hours: float = 5.0,
    labels: list[str] | None = None,
    author: str = "",
    body: str = "",
) -> Issue:
    return Issue(
        number=number,
        title=title,
        state=state,
        created_at=utcnow() - timedelta(hours=age_hours),
        html_url=f"https://github.com/NousResearch/hermes-agent/issues/{number}",
        labels=labels or ["bug"],
        author=author,
        body=body,
    )


class FakeGitHubClient:
    """Stand-in for GitHubClient: full control, zero network.

    ``compare`` answers release-to-release diffs; ``head_compare`` answers the
    tag-to-HEAD probe used by the provenance model (``None`` = "compare API
    unavailable", which the provenance model reports honestly instead of
    guessing).
    """

    def __init__(
        self,
        *,
        releases: list[Release] | None = None,
        compare: CompareResult | None = None,
        head_compare: CompareResult | None = None,
        search: dict[str, tuple[int, list[Issue]]] | None = None,
        comments: dict[int, list] | None = None,
        token: str | None = None,
    ) -> None:
        self.releases = releases or []
        self.compare_result = compare
        self.head_compare_result = head_compare
        self.search_results = search or {}
        self.comment_results = comments or {}
        self.degradations: list[str] = []
        self.searches_used = 0
        self.comment_calls: list[int] = []
        self.http = type("FakeHttp", (), {"token": token})()

    def list_releases(self, *, per_page: int = 20, use_cache: bool = True):
        return list(self.releases)

    def latest_release(self, *, include_prereleases: bool = False, per_page: int = 20):
        for release in self.releases:
            if release.prerelease and not include_prereleases:
                continue
            return release
        return None

    def compare(self, base_tag: str, head_tag: str, *, use_cache: bool = True):
        # tag...tag -> release diff; tag...sha/branch -> provenance probe
        if base_tag.startswith("v2") and head_tag and not head_tag.startswith("v2"):
            return self.head_compare_result
        if head_tag in {"main", "master"}:
            return self.head_compare_result
        return self.compare_result

    def search_issues(
        self, query: str, *, per_page: int = 50, sort: str = "created", order: str = "desc", use_cache: bool = True
    ):
        from hermes_update_check.github_api import IssueSearchResult

        self.searches_used += 1
        for key, (total, items) in self.search_results.items():
            if key in query:
                return IssueSearchResult(query=query, total_count=total, items=list(items)[:per_page], ok=True)
        return IssueSearchResult(query=query, total_count=0, items=[], ok=True)

    def get_issue_comments(self, number: int, *, per_page: int = 20, use_cache: bool = True):

        self.comment_calls.append(number)
        return list(self.comment_results.get(number, []))

    def get_rate_limit(self):
        return {"resources": {"core": {"limit": 60, "remaining": 55}, "search": {"limit": 10, "remaining": 7}}}


@pytest.fixture()
def fake_client() -> FakeGitHubClient:
    return FakeGitHubClient()


def dump_json(path: Path, data) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path
