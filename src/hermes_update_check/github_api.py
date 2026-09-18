"""GitHub API access for the Hermes repository: releases, compares, issue searches.

Every method is defensive: on failure it returns None / an empty result and
records why, because "the API was down" must become *INSUFFICIENT DATA* in the
report - never "looks safe".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence
from urllib.parse import quote_plus

from .http import HttpClient
from .logging_setup import get_logger
from .util import parse_iso8601

API_ROOT = "https://api.github.com"

#: GitHub truncates compare responses at 250 commits; ``total_commits`` stays truthful.
COMPARE_COMMIT_LIMIT = 250

#: Unauthenticated search queries are capped at 256 characters (384 when authenticated).
MAX_QUERY_LENGTH = 250

#: The search API rejects queries using more than five AND/OR/NOT operators
#: (HTTP 422 "Validation Failed"). Six OR'ed terms = five operators.
MAX_OR_TERMS = 6


@dataclass
class Release:
    """One GitHub release."""

    tag: str
    name: str = ""
    body: str = ""
    html_url: str = ""
    published_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    prerelease: bool = False
    draft: bool = False
    author: str = ""
    display_version: Optional[str] = None

    @property
    def when(self) -> Optional[datetime]:
        return self.published_at or self.created_at

    @property
    def age_hours(self) -> Optional[float]:
        when = self.when
        if when is None:
            return None
        from .util import hours_between

        return hours_between(when)

    @property
    def age_days(self) -> Optional[float]:
        hours = self.age_hours
        return None if hours is None else hours / 24.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "name": self.name,
            "display_version": self.display_version,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "prerelease": self.prerelease,
            "draft": self.draft,
            "html_url": self.html_url,
            "age_hours": round(self.age_hours, 2) if self.age_hours is not None else None,
            "body_length": len(self.body or ""),
        }


@dataclass
class CommitInfo:
    sha: str
    message: str = ""
    date: Optional[datetime] = None

    @property
    def subject(self) -> str:
        return (self.message or "").splitlines()[0].strip() if self.message else ""

    @property
    def pr_number(self) -> Optional[int]:
        match = re.search(r"\(#(\d+)\)", self.subject)
        if match:
            return int(match.group(1))
        match = re.search(r"\bmerge pull request #(\d+)", self.message or "", re.IGNORECASE)
        return int(match.group(1)) if match else None


@dataclass
class CompareResult:
    base_tag: str
    head_tag: str
    status: str = "unknown"
    total_commits: int = 0
    ahead_by: int = 0
    behind_by: int = 0
    commits: list[CommitInfo] = field(default_factory=list)
    files_changed: int = 0
    html_url: str = ""

    @property
    def truncated(self) -> bool:
        return self.total_commits > len(self.commits)

    @property
    def pr_numbers(self) -> list[int]:
        seen: list[int] = []
        for commit in self.commits:
            number = commit.pr_number
            if number is not None and number not in seen:
                seen.append(number)
        return seen

    @property
    def commit_subjects(self) -> list[str]:
        return [c.subject for c in self.commits if c.subject]

    @property
    def commit_corpus(self) -> str:
        return "\n".join(self.commit_subjects)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_tag": self.base_tag,
            "head_tag": self.head_tag,
            "status": self.status,
            "total_commits": self.total_commits,
            "commits_in_payload": len(self.commits),
            "truncated": self.truncated,
            "files_changed": self.files_changed,
            "pr_numbers_seen": len(self.pr_numbers),
            "html_url": self.html_url,
        }


@dataclass
class Issue:
    number: int
    title: str
    state: str = "open"
    created_at: Optional[datetime] = None
    html_url: str = ""
    labels: list[str] = field(default_factory=list)
    comments: int = 0
    #: extra fields used by the regression-credibility analysis
    author: str = ""
    author_association: str = ""
    body: str = ""
    updated_at: Optional[datetime] = None

    @property
    def label_text(self) -> str:
        return ", ".join(self.labels)

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "state": self.state,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "html_url": self.html_url,
            "labels": self.labels,
            "comments": self.comments,
            "author": self.author,
            "author_association": self.author_association,
        }


#: Repository associations that mean "this person works on the project".
MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def is_maintainer_association(value: str | None) -> bool:
    return str(value or "").strip().upper() in MAINTAINER_ASSOCIATIONS


@dataclass
class IssueComment:
    author: str = ""
    body: str = ""
    author_association: str = ""
    created_at: Optional[datetime] = None

    @property
    def from_maintainer(self) -> bool:
        return is_maintainer_association(self.author_association)

    def to_dict(self) -> dict[str, Any]:
        return {
            "author": self.author,
            "author_association": self.author_association,
            "from_maintainer": self.from_maintainer,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass
class IssueSearchResult:
    query: str = ""
    total_count: int = 0
    items: list[Issue] = field(default_factory=list)
    ok: bool = False
    degraded: bool = False
    error: Optional[str] = None
    from_cache: bool = False


class GitHubClient:
    """Thin, typed wrapper over the three endpoints this tool needs."""

    def __init__(
        self,
        *,
        repo: str = "NousResearch/hermes-agent",
        http: Optional[HttpClient] = None,
        max_issue_searches: int = 3,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.repo = repo
        self.http = http or HttpClient()
        self.max_issue_searches = max(1, int(max_issue_searches))
        self.log = logger or get_logger("github")
        self._searches_used = 0
        self.degradations: list[str] = []

    # -- endpoints ----------------------------------------------------------- #

    def list_releases(self, *, per_page: int = 20, use_cache: bool = True) -> list[Release]:
        """Newest-first list of releases (includes prereleases; filter at the call site)."""
        url = f"{API_ROOT}/repos/{self.repo}/releases"
        result = self.http.get_json(url, params={"per_page": max(1, min(100, per_page))}, use_cache=use_cache)
        if result.stale:
            self._record_degradation(f"releases: using cached data ({result.error})")
        if not result.ok:
            self._record_degradation(f"releases unavailable: {result.error}")
            return []
        if not isinstance(result.data, list):
            self._record_degradation("releases payload was not a list")
            return []
        releases = [self._parse_release(item) for item in result.data if isinstance(item, dict)]
        return [r for r in releases if r.tag]

    def latest_release(self, *, include_prereleases: bool = False, per_page: int = 20) -> Optional[Release]:
        """Newest non-draft release, stable-only unless ``include_prereleases``."""
        for release in self.list_releases(per_page=per_page):
            if release.draft:
                continue
            if release.prerelease and not include_prereleases:
                continue
            return release
        return None

    def compare(self, base_tag: str, head_tag: str, *, use_cache: bool = True) -> Optional[CompareResult]:
        """Compare two tags/refs; handles the 250-commit truncation honestly."""
        if not base_tag or not head_tag:
            return None
        url = f"{API_ROOT}/repos/{self.repo}/compare/{quote_plus(base_tag)}...{quote_plus(head_tag)}"
        result = self.http.get_json(url, use_cache=use_cache)
        if result.stale:
            self._record_degradation(f"compare {base_tag}...{head_tag}: using cached data ({result.error})")
        if not result.ok:
            self._record_degradation(f"compare {base_tag}...{head_tag} unavailable: {result.error}")
            return None
        data = result.data
        if not isinstance(data, dict):
            self._record_degradation("compare payload malformed")
            return None
        commits = []
        for raw in data.get("commits") or []:
            if not isinstance(raw, dict):
                continue
            commit = raw.get("commit") or {}
            author = commit.get("author") or {}
            commits.append(
                CommitInfo(
                    sha=str(raw.get("sha", ""))[:40],
                    message=str(commit.get("message", "")),
                    date=parse_iso8601(author.get("date")),
                )
            )
        compare = CompareResult(
            base_tag=base_tag,
            head_tag=head_tag,
            status=str(data.get("status", "unknown")),
            total_commits=int(data.get("total_commits") or len(commits)),
            ahead_by=int(data.get("ahead_by") or 0),
            behind_by=int(data.get("behind_by") or 0),
            commits=commits,
            files_changed=len(data.get("files") or []),
            html_url=str(data.get("html_url", "")),
        )
        if compare.truncated:
            self.log.debug("compare truncated: %d of %d commits in payload", len(commits), compare.total_commits)
        return compare

    def tag_commit(self, tag: str, *, use_cache: bool = True) -> Optional[str]:
        """The commit a release tag points at (annotated tags are dereferenced).

        Phase-4 local-git fallback: when GitHub cannot *compare* (404 - the local
        commit was never pushed, or the tag was not fetched), the release *commit*
        can still be compared with the local object store. None when unknown.
        """
        if not tag:
            return None
        url = f"{API_ROOT}/repos/{self.repo}/git/ref/tags/{quote_plus(tag)}"
        result = self.http.get_json(url, use_cache=use_cache)
        if not result.ok or not isinstance(result.data, dict):
            return None
        obj = result.data.get("object")
        if not isinstance(obj, dict):
            return None
        sha = str(obj.get("sha", ""))
        kind = str(obj.get("type", ""))
        if kind == "tag" and sha:
            # annotated tag: the ref points at a tag object, not at the commit
            deref = self.http.get_json(f"{API_ROOT}/repos/{self.repo}/git/tags/{sha}", use_cache=use_cache)
            if deref.ok and isinstance(deref.data, dict):
                inner = deref.data.get("object")
                if isinstance(inner, dict) and inner.get("sha"):
                    return str(inner["sha"])
        return sha or None

    def search_issues(
        self,
        query: str,
        *,
        per_page: int = 50,
        sort: str = "created",
        order: str = "desc",
        use_cache: bool = True,
    ) -> IssueSearchResult:
        """Issue search with a hard per-run cap (search API is rate limited hard)."""
        if self._searches_used >= self.max_issue_searches:
            return IssueSearchResult(
                query=query,
                ok=False,
                degraded=True,
                error=f"search budget exhausted ({self.max_issue_searches} per run)",
            )
        self._searches_used += 1

        url = f"{API_ROOT}/search/issues"
        result = self.http.get_json(
            url,
            params={"q": query, "sort": sort, "order": order, "per_page": max(1, min(100, per_page))},
            use_cache=use_cache,
        )
        if result.stale:
            self._record_degradation(f"issue search: using cached data ({result.error})")
        if not result.ok:
            self._record_degradation(f"issue search failed: {result.error}")
            return IssueSearchResult(
                query=query, ok=False, degraded=True, error=result.error, from_cache=result.from_cache
            )
        data = result.data
        if not isinstance(data, dict):
            self._record_degradation("issue search payload malformed")
            return IssueSearchResult(query=query, ok=False, degraded=True, error="malformed payload")

        items = [self._parse_issue(raw) for raw in (data.get("items") or []) if isinstance(raw, dict)]
        return IssueSearchResult(
            query=query,
            total_count=int(data.get("total_count") or 0),
            items=items,
            ok=True,
            degraded=result.stale,
            from_cache=result.from_cache,
        )

    def get_rate_limit(self) -> dict[str, Any]:
        return self.http.probe_rate_limit()

    # -- parsing ------------------------------------------------------------- #

    def _parse_release(self, raw: dict[str, Any]) -> Release:
        tag = str(raw.get("tag_name") or "").strip()
        name = str(raw.get("name") or "")
        body = str(raw.get("body") or "")
        return Release(
            tag=tag,
            name=name,
            body=body,
            html_url=str(raw.get("html_url") or ""),
            published_at=parse_iso8601(raw.get("published_at")),
            created_at=parse_iso8601(raw.get("created_at")),
            prerelease=bool(raw.get("prerelease")),
            draft=bool(raw.get("draft")),
            author=str((raw.get("author") or {}).get("login") or ""),
            display_version=extract_display_version(name, body, tag),
        )

    def _parse_issue(self, raw: dict[str, Any]) -> Issue:
        labels = []
        for label in raw.get("labels") or []:
            if isinstance(label, dict) and label.get("name"):
                labels.append(str(label["name"]))
            elif isinstance(label, str):
                labels.append(label)
        user = raw.get("user") or {}
        body = str(raw.get("body") or "")
        return Issue(
            number=int(raw.get("number") or 0),
            title=str(raw.get("title") or ""),
            state=str(raw.get("state") or "open"),
            created_at=parse_iso8601(raw.get("created_at")),
            html_url=str(raw.get("html_url") or ""),
            labels=labels,
            comments=int(raw.get("comments") or 0),
            author=str(user.get("login") or ""),
            author_association=str(raw.get("author_association") or ""),
            body=body[:4000],
            updated_at=parse_iso8601(raw.get("updated_at")),
        )

    def get_issue_comments(self, number: int, *, per_page: int = 20, use_cache: bool = True) -> list[IssueComment]:
        """Comments of one issue (used for maintainer-confirmation checks).

        Costs one core API call per issue, so callers must bound how many issues
        they enrich - see ``github.issue_enrichment_limit``.
        """
        if number <= 0:
            return []
        url = f"{API_ROOT}/repos/{self.repo}/issues/{number}/comments"
        result = self.http.get_json(url, params={"per_page": max(1, min(100, per_page))}, use_cache=use_cache)
        if not result.ok:
            if not result.from_cache:
                self._record_degradation(f"issue #{number} comments unavailable: {result.error}")
            return []
        if not isinstance(result.data, list):
            return []
        comments: list[IssueComment] = []
        for raw in result.data:
            if not isinstance(raw, dict):
                continue
            user = raw.get("user") or {}
            comments.append(
                IssueComment(
                    author=str(user.get("login") or ""),
                    body=str(raw.get("body") or ""),
                    author_association=str(raw.get("author_association") or ""),
                    created_at=parse_iso8601(raw.get("created_at")),
                )
            )
        return comments

    def _record_degradation(self, message: str) -> None:
        if message not in self.degradations:
            self.degradations.append(message)
        self.log.warning("%s", message)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

_VERSION_IN_NAME_RE = re.compile(r"v?(\d+\.\d+\.\d+(?:[-_.]?(?:rc|beta|alpha|dev)\.?\d*)?)")


def extract_display_version(name: str, body: str, tag: str) -> Optional[str]:
    """Find the SemVer-ish display version behind a date tag.

    ``"Hermes Agent v0.21.3 (v2026.9.14)"`` -> ``0.21.3``; falls back to a
    ``v0.21.3`` mention in the body, then to the tag itself when the tag *is*
    a semver (some repositories tag by semver only).
    """
    for candidate in (name, body[:4000]):
        if not candidate:
            continue
        for match in _VERSION_IN_NAME_RE.finditer(candidate):
            token = match.group(1)
            # Skip the date-like tag itself (2026.9.14 would match \d+\.\d+\.\d+).
            if _looks_like_date_tag(token) and tag.endswith(token.replace("v", "")):
                continue
            return token.lstrip("v")
    if re.fullmatch(r"v?\d+\.\d+\.\d+", tag or ""):
        return tag.lstrip("v")
    return None


def _looks_like_date_tag(token: str) -> bool:
    parts = token.split(".")
    if len(parts) != 3:
        return False
    try:
        year, month, day = (int(p) for p in parts)
    except ValueError:
        return False
    return 2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31


_PR_COUNT_RE = re.compile(
    r"(?:rolls?\s+up|rollup|contains|merges?)\D{0,24}?~?(\d{1,5})\s*(?:PRs?|pull requests?)",
    re.IGNORECASE,
)
_BARE_PR_COUNT_RE = re.compile(r"~?(\d{1,5})\s*(?:PRs|pull requests?)\b", re.IGNORECASE)


def extract_pr_count(text: str | None) -> Optional[int]:
    """Estimate the number of PRs a release bundles, from its own release notes.

    The compare API is truncated at 250 commits, so counting ``(#1234)``
    occurrences systematically *under*-reports the size of a big rollup;
    release notes usually state the true number ("rolls up the ~338 PRs").
    """
    if not text:
        return None
    for pattern in (_PR_COUNT_RE, _BARE_PR_COUNT_RE):
        best: Optional[int] = None
        for match in pattern.finditer(text):
            try:
                value = int(match.group(1))
            except ValueError:
                continue
            if value <= 0 or value > 50000:
                continue
            best = value if best is None else max(best, value)
        if best is not None:
            return best
    return None


def build_issue_query(
    repo: str,
    *,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
    keywords: Sequence[str] = (),
    label: Optional[str] = None,
    state: Optional[str] = None,
) -> str:
    """Compose a GitHub issue-search query string."""
    parts = [f"repo:{repo}", "type:issue"]
    if label:
        parts.append(f'label:"{label}"')
    if state:
        parts.append(f"state:{state}")
    if created_after:
        # Date-only keeps the query valid for the search API on every client
        # (timestamps in `created:` are not universally accepted).
        parts.append(f"created:>={created_after.strftime('%Y-%m-%d')}")
    if created_before:
        parts.append(f"created:<{created_before.strftime('%Y-%m-%d')}")

    query = " ".join(parts)
    if keywords:
        # Two hard limits apply to search queries: 256 characters and at most
        # five boolean operators (=> at most six OR'ed terms).
        terms = list(keywords)[:MAX_OR_TERMS]
        while terms:
            candidate = f"{query} in:title " + " OR ".join(f'"{kw}"' if " " in kw else kw for kw in terms)
            if len(candidate) <= MAX_QUERY_LENGTH or len(terms) == 1:
                query = candidate
                break
            terms.pop()
    return query


def parse_repo_slug(slug: str) -> tuple[str, str]:
    owner, _, name = slug.partition("/")
    if not owner or not name:
        raise ValueError(f"invalid repo slug: {slug!r} (expected 'owner/name')")
    return owner, name
