"""Orchestration: gather every input, then hand it to the risk engine.

`run_check()` is the single entry point used by ``check``, ``report``,
``watch`` and ``update`` (via ``--dry-run``) so all four always agree.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .advisor import Recommendation, advise
from .clusters import (
    DEFAULT_ENRICHMENT_LIMIT,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    IssueEnrichment,
    RegressionCluster,
    build_clusters,
)
from .config import Config, resolve_state_dir
from .gates import EnvironmentState, GateReport, evaluate_gates, probe_environment
from .impact import PersonalReadiness, compute_personal_readiness, local_rollback_risk
from .rollback_safety import RollbackSafety, assess_rollback_safety
from .usage_profile import UsageProfile, resolve_profile
from .github_api import (
    CompareResult,
    GitHubClient,
    Issue,
    IssueSearchResult,
    Release,
    build_issue_query,
    extract_pr_count,
)
from .http import DiskCache, HttpClient
from .local_env import LocalEnv, detect_local_env
from .logging_setup import get_logger
from .provenance import (
    CHANNEL_STABLE,
    UPDATE_STATUS_UP_TO_DATE,
    CodeProvenance,
    UpdateDecision,
    decide_update,
    resolve_provenance,
)
from .risk import CheckContext, IssueSignal, RiskAssessment, assess, classify_issues, level_for_score
from .util import hours_between, iso, utcnow
from .versioning import compare_versions, normalise_tag

#: Title keywords used for the post-release issue search. Two hard GitHub
#: limits shape this list: at most five boolean operators (six OR'ed terms)
#: and 256 characters for the whole query.
ISSUE_KEYWORDS: tuple[str, ...] = (
    "bug",
    "crash",
    "corrupt",
    "regression",
    "broken",
    "failed",
)

#: Extra query that catches bug-labelled reports without the keyword noise.
ISSUE_FALLBACK_QUERY = "label:bug"

BASELINE_MIN_DAYS = 3.0
BASELINE_MAX_DAYS = 14.0


@dataclass
class UpdateCheck:
    """The complete result of one check - everything the report needs."""

    cfg: Config
    env: LocalEnv
    generated_at: datetime = field(default_factory=utcnow)
    releases: list[Release] = field(default_factory=list)
    latest: Optional[Release] = None
    previous: Optional[Release] = None
    compare: Optional[CompareResult] = None
    versions_behind: Optional[int] = None
    update_available: Optional[bool] = None
    target_version: Optional[str] = None
    target_tag: Optional[str] = None
    pr_count: Optional[int] = None
    pr_count_from_notes: bool = False
    issues: Optional[IssueSignal] = None
    assessment: Optional[RiskAssessment] = None
    degradation: list[str] = field(default_factory=list)
    main_ahead_commits: Optional[int] = None
    rate_limit: dict[str, Any] = field(default_factory=dict)
    token_used: bool = False
    # -- phase 2 additions --------------------------------------------------- #
    provenance: Optional[CodeProvenance] = None
    decision: Optional[UpdateDecision] = None
    clusters: list[RegressionCluster] = field(default_factory=list)
    environment: Optional[EnvironmentState] = None
    gates: Optional[GateReport] = None
    recommendation: Optional[Recommendation] = None
    # -- phase 3 additions --------------------------------------------------- #
    profile: Optional[UsageProfile] = None
    readiness: Optional[PersonalReadiness] = None
    rollback_safety: Optional[RollbackSafety] = None

    @property
    def degraded(self) -> bool:
        return bool(self.degradation)

    @property
    def update_status(self) -> str:
        if self.decision is not None:
            return self.decision.status
        if self.update_available is False:
            return UPDATE_STATUS_UP_TO_DATE
        return "unknown"

    @property
    def channel(self) -> str:
        return self.provenance.channel if self.provenance else "UNKNOWN"

    @property
    def action(self) -> str:
        """The final action (advisor), falling back to the legacy recommendation."""
        if self.recommendation is not None:
            return self.recommendation.action
        if self.update_available is False:
            return "UP_TO_DATE"
        return self.assessment.recommendation if self.assessment else "INSUFFICIENT_DATA"

    @property
    def tracking_main(self) -> bool:
        return bool(self.env.tracking_main)

    @property
    def recommended_action(self) -> str:
        return self.action

    def headline_rows(self, *, lang: str = "zh") -> list[tuple[str, str]]:
        """Rows for the report's top table."""
        prov = self.provenance
        rows: list[tuple[str, str]] = []
        if prov is not None:
            rows.append(("Reported Version", f"v{prov.reported_version}" if prov.reported_version else "unknown"))
            rows.append(("Channel", prov.channel))
            if prov.git_branch:
                rows.append(("Git Branch", prov.git_branch))
            if prov.git_commit:
                rows.append(("Git Commit", prov.git_commit))
            if prov.nearest_tag:
                rows.append(("Nearest Release", prov.nearest_tag))
            if prov.commits_ahead_of_tag:
                rows.append(
                    (
                        "Ahead of Stable",
                        f"{prov.commits_ahead_of_tag} commits"
                        if lang == "en"
                        else f"{prov.commits_ahead_of_tag} 个 commit",
                    )
                )
            if prov.commits_behind_target:
                rows.append(("Behind Latest", f"{prov.commits_behind_target} commits"))
            if prov.is_git_install:
                rows.append(
                    (
                        "Working Tree",
                        ("dirty" if prov.dirty_worktree else "clean")
                        if lang == "en"
                        else (f"有 {prov.dirty_files} 个未提交修改" if prov.dirty_worktree else "干净"),
                    )
                )
        else:
            rows.append(("Current Version", self.env.version_label))
        if self.latest is not None:
            latest_label = f"v{self.latest.display_version}" if self.latest.display_version else self.latest.tag
            rows.append(
                (
                    "Latest Version",
                    f"{latest_label} ({self.latest.tag})" if self.latest.tag not in latest_label else latest_label,
                )
            )
            if self.latest.when:
                age = self.latest.age_hours or 0.0
                days = age / 24.0
                rows.append(
                    ("Released", f"{iso(self.latest.when)} ({days:.1f} {'days' if lang == 'en' else '天'} ago)")
                )
            if self.latest.html_url:
                rows.append(("Release page", self.latest.html_url))
        if self.compare is not None:
            commits = f"{self.compare.total_commits}" + ("+" if self.compare.truncated else "")
            rows.append(("Commits", commits))
            if self.pr_count is not None:
                suffix = (
                    (" (from release notes)" if lang == "en" else "（来自 Release Notes）")
                    if self.pr_count_from_notes
                    else ("+" if self.compare.truncated else "")
                )
                rows.append(("PRs", f"{self.pr_count}{suffix}"))
        if self.versions_behind is not None:
            rows.append(("Releases behind", str(self.versions_behind)))
        if self.decision is not None:
            rows.append(
                (
                    "Update Status",
                    self.decision.headline_zh if lang == "zh" else self.decision.headline_en,
                )
            )
        if self.assessment is not None:
            rows.append(("Risk Score", self.assessment.score_or_unknown))
            rows.append(("Risk Level", self.assessment.level))
            if self.assessment.stability is not None:
                rows.append(("Stability", f"{self.assessment.stability}/100"))
            if self.assessment.data_confidence is not None:
                rows.append(("Data confidence", f"{self.assessment.data_confidence}%"))
        if self.main_ahead_commits is not None:
            rows.append(("Main ahead of release", f"{self.main_ahead_commits} commits"))
        return rows

    def to_dict(self) -> dict[str, Any]:
        """JSON contract - the phase-2 keys are additive, nothing was removed."""
        local = self.env.to_dict()
        if self.provenance is not None:
            local.update(self.provenance.to_dict())
        assessment = self.assessment
        release_block: dict[str, Any] = {}
        if self.latest is not None:
            release_block = self.latest.to_dict()
            release_block.update(
                {
                    "previous_tag": self.previous.tag if self.previous else None,
                    "pr_count": self.pr_count,
                    "pr_count_from_notes": self.pr_count_from_notes,
                    "compare": self.compare.to_dict() if self.compare else None,
                }
            )
        return {
            "generated_at": iso(self.generated_at),
            "repo": self.cfg.repo,
            "local": local,
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "release": release_block,
            "latest_release": self.latest.to_dict() if self.latest else None,
            "previous_release": self.previous.to_dict() if self.previous else None,
            "compare": self.compare.to_dict() if self.compare else None,
            "versions_behind": self.versions_behind,
            "update_available": self.update_available,
            "update_status": self.update_status,
            "update_decision": self.decision.to_dict() if self.decision else None,
            "target_version": self.target_version,
            "target_tag": self.target_tag,
            "pr_count": self.pr_count,
            "pr_count_from_notes": self.pr_count_from_notes,
            "main_ahead_commits": self.main_ahead_commits,
            "channel": self.channel,
            "risk": {
                "change_risk": assessment.change_risk if assessment else None,
                "regression_signal": assessment.regression_signal if assessment else None,
                "data_confidence": assessment.data_confidence if assessment else None,
                "environment_risk": assessment.environment_risk if assessment else None,
                "overall": assessment.overall if assessment else None,
                "stability": assessment.stability if assessment else None,
                "level": assessment.level if assessment else None,
                "score_is_lower_bound": bool(assessment.score_is_lower_bound) if assessment else False,
                "confidence": round(assessment.confidence, 3) if assessment else None,
            },
            "assessment": assessment.to_dict() if assessment else None,
            "hard_gates": [g.to_dict() for g in (self.gates.gates if self.gates else [])],
            "hard_gates_summary": self.gates.to_dict() if self.gates else None,
            "regressions": [c.to_dict() for c in self.clusters],
            "issues": self.issues.to_dict() if self.issues else None,
            # -- phase 3 ------------------------------------------------------- #
            "personal_readiness": self.readiness.to_dict() if self.readiness else None,
            "rollback_safety": self.rollback_safety.to_dict() if self.rollback_safety else None,
            "usage_profile": self.profile.to_dict() if self.profile else None,
            "recommendation": self.action,
            "recommendation_detail": self.recommendation.to_dict() if self.recommendation else None,
            "recommended_recheck": iso(self.recommendation.recheck_at) if self.recommendation else None,
            "recommended_recheck_hours": (
                round(self.recommendation.recheck_hours, 2)
                if self.recommendation and self.recommendation.recheck_hours is not None
                else None
            ),
            "policy_clearance": iso(self.recommendation.policy_clearance_at) if self.recommendation else None,
            "policy_clearance_hours": (
                round(self.recommendation.policy_clearance_hours, 2)
                if self.recommendation and self.recommendation.policy_clearance_hours is not None
                else None
            ),
            "degradation": self.degradation,
            "rate_limit": self.rate_limit,
            "token_used": self.token_used,
        }


# --------------------------------------------------------------------------- #
# main entry point
# --------------------------------------------------------------------------- #


def build_http_client(
    cfg: Config, state_root: Path, *, logger: Optional[logging.Logger] = None, no_cache: bool = False
) -> tuple[HttpClient, bool]:
    """Create the HTTP client with token and disk cache. Returns (client, token_used)."""
    token, token_used = resolve_github_token(cfg)
    cache = None if no_cache else DiskCache(state_root / "cache", cfg.github.cache_ttl_minutes, logger=logger)
    client = HttpClient(
        timeout=cfg.network.timeout_seconds,
        retries=cfg.network.retries,
        cache=cache,
        token=token,
        logger=logger,
    )
    return client, token_used


def resolve_github_token(cfg: Config) -> tuple[Optional[str], bool]:
    """Find a GitHub token in the environment (never in the config file)."""
    import os

    candidates = [cfg.github.token_env, *cfg.github.token_env_fallbacks]
    for name in candidates:
        if not name:
            continue
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip(), True
    return None, False


def build_github_client(
    cfg: Config,
    state_root: Path,
    *,
    logger: Optional[logging.Logger] = None,
    no_cache: bool = False,
) -> tuple[GitHubClient, bool, HttpClient]:
    http, token_used = build_http_client(cfg, state_root, logger=logger, no_cache=no_cache)
    client = GitHubClient(
        repo=cfg.github.repo,
        http=http,
        max_issue_searches=cfg.github.max_issue_searches,
        logger=logger,
    )
    return client, token_used, http


def run_check(
    cfg: Config,
    *,
    env: Optional[LocalEnv] = None,
    client: Optional[GitHubClient] = None,
    state_root: Optional[Path] = None,
    no_cache: bool = False,
    include_issues: Optional[bool] = None,
    logger: Optional[logging.Logger] = None,
) -> UpdateCheck:
    """Full check: local env -> releases -> compare -> issues -> risk assessment."""
    log = logger or get_logger("check")
    root = state_root or resolve_state_dir(cfg)
    token_used = False
    if client is None:
        client, token_used, _http = build_github_client(cfg, root, logger=log, no_cache=no_cache)
    else:
        token_used = bool(getattr(client.http, "token", None))

    local = env or detect_local_env(cfg, logger=log)
    check = UpdateCheck(cfg=cfg, env=local, token_used=token_used)
    check.degradation.extend(local.errors)

    include_prerelease = bool(cfg.allow_prerelease or cfg.preferred_channel == "prerelease")
    releases = client.list_releases(per_page=30, use_cache=not no_cache)
    check.degradation.extend(client.degradations)
    client.degradations.clear()
    check.releases = releases

    if not releases:
        check.degradation.append("no release data available (GitHub unreachable and no usable cache)")
        check.assessment = assess(
            CheckContext(
                local_version=local.version,
                local_tag=local.release_tag,
                tracking_main=local.tracking_main,
                install_kind=local.install_kind,
                dirty_tree=bool(local.git and local.git.dirty),
                latest_version=None,
                latest_tag=None,
                release_age_hours=None,
                weights=cfg.risk,
                risk_threshold=cfg.risk_threshold,
                minimum_release_age_days=cfg.minimum_release_age_days,
                issues_enabled=cfg.check_github_issues,
            )
        )
        return check

    latest = _pick_latest(releases, include_prerelease=include_prerelease)
    check.latest = latest
    if latest is None:
        check.degradation.append("no stable release found in the release list")
        return check

    check.target_version = latest.display_version
    check.target_tag = latest.tag
    check.previous = _pick_previous(releases, latest)
    check.versions_behind = _releases_behind(releases, local.version)

    # -- code provenance: what is actually running? ------------------------ #
    provenance = resolve_provenance(
        local,
        releases,
        latest=latest,
        compare=lambda base, head: client.compare(base, head, use_cache=not no_cache),
    )
    check.degradation.extend(client.degradations)
    client.degradations.clear()
    check.provenance = provenance
    decision = decide_update(
        provenance,
        latest,
        preferred_channel=cfg.preferred_channel,
        allow_prerelease=bool(cfg.allow_prerelease or cfg.preferred_channel == "prerelease"),
    )
    check.decision = decision
    check.update_available = decision.is_update_candidate
    check.main_ahead_commits = provenance.commits_ahead_of_tag if provenance.channel != CHANNEL_STABLE else None

    # -- diff size -------------------------------------------------------- #
    base_tag = check.previous.tag if check.previous else (local.release_tag or None)
    if base_tag and base_tag != latest.tag:
        check.compare = client.compare(base_tag, latest.tag, use_cache=not no_cache)
        check.degradation.extend(client.degradations)
        client.degradations.clear()

    # The compare API truncates at 250 commits, so the PR count seen in commit
    # subjects is a floor; prefer the number the release notes state.
    pr_from_subjects = len(check.compare.pr_numbers) if check.compare else 0
    pr_from_notes = extract_pr_count(latest.body)
    if pr_from_notes and pr_from_notes >= pr_from_subjects:
        check.pr_count = pr_from_notes
        check.pr_count_from_notes = True
    elif pr_from_subjects:
        check.pr_count = pr_from_subjects

    # -- issues ------------------------------------------------------------ #
    issues_enabled = cfg.check_github_issues if include_issues is None else bool(include_issues)
    if issues_enabled and latest.published_at is not None:
        check.issues = collect_issue_signal(
            cfg, client, latest, provenance=provenance, logger=log, use_cache=not no_cache
        )
        check.degradation.extend(client.degradations)
        client.degradations.clear()
        if check.issues is not None and check.issues.unavailable_reason:
            check.degradation.append(f"issue data unavailable: {check.issues.unavailable_reason}")
    elif issues_enabled:
        check.issues = IssueSignal(unavailable_reason="release has no publication timestamp")

    check.clusters = list(check.issues.clusters) if check.issues is not None else []

    # -- risk -------------------------------------------------------------- #
    ctx = CheckContext(
        local_version=local.version,
        local_tag=local.release_tag,
        tracking_main=local.tracking_main,
        install_kind=local.install_kind,
        dirty_tree=bool(local.git and local.git.dirty),
        latest_version=latest.display_version,
        latest_tag=latest.tag,
        latest_prerelease=latest.prerelease,
        release_age_hours=latest.age_hours,
        release_body=latest.body,
        release_name=latest.name,
        total_commits=(check.compare.total_commits if check.compare else None),
        commit_count_known=check.compare is not None,
        pr_count=check.pr_count,
        versions_behind=check.versions_behind,
        commit_subjects=(check.compare.commit_subjects if check.compare else []),
        issues=check.issues,
        issues_enabled=issues_enabled,
        provenance_ok=bool(local.version or provenance.git_commit) and provenance.channel != "UNKNOWN",
        provenance_channel=provenance.channel,
        weights=cfg.risk,
        risk_threshold=cfg.risk_threshold,
        minimum_release_age_days=cfg.minimum_release_age_days,
        degraded_inputs=check.degradation,
    )
    check.assessment = assess(ctx)

    # -- phase 3: personal impact / readiness / systemic risk / rollback ------ #
    check.profile = resolve_profile(cfg, hermes_home=local.hermes_home)
    check.readiness = compute_personal_readiness(check.profile, check.clusters)
    check.rollback_safety = assess_rollback_safety(
        cfg,
        local,
        provenance,
        state_dir=root,
        logger=log,
    )
    local_risk = local_rollback_risk(
        check.rollback_safety.status,
        detail_zh="；".join(check.rollback_safety.reasons_zh[:2]),
        detail_en="; ".join(check.rollback_safety.reasons_en[:2]),
    )
    if local_risk is not None:
        check.readiness.systemic.append(local_risk)
        check.readiness.reasons_zh.append(f"本地回滚检查未通过：{local_risk.evidence_zh}")
        check.readiness.reasons_en.append(f"local rollback check failed: {local_risk.evidence_en}")

    # -- hard gates + final recommendation --------------------------------- #
    check.environment = probe_environment(local, logger=log)
    check.gates = evaluate_gates(
        cfg,
        provenance=provenance,
        decision=decision,
        release=latest,
        assessment=check.assessment,
        clusters=check.clusters,
        environment=check.environment,
        readiness=check.readiness,
        rollback_safety=check.rollback_safety,
    )
    check.recommendation = advise(
        cfg,
        provenance=provenance,
        decision=decision,
        assessment=check.assessment,
        gates=check.gates,
        clusters=check.clusters,
        release=latest,
        readiness=check.readiness,
    )

    try:
        check.rate_limit = client.get_rate_limit()
    except Exception:  # pragma: no cover - purely informational
        check.rate_limit = {}
    return check


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _pick_latest(releases: list[Release], *, include_prerelease: bool) -> Optional[Release]:
    for release in releases:
        if release.draft:
            continue
        if release.prerelease and not include_prerelease:
            continue
        return release
    return None


def _pick_previous(releases: list[Release], latest: Release) -> Optional[Release]:
    try:
        index = releases.index(latest)
    except ValueError:
        return None
    for release in releases[index + 1 :]:
        if not release.draft:
            return release
    return None


def _releases_behind(releases: list[Release], current_version: Optional[str]) -> Optional[int]:
    """How many releases separate the installed version from the newest one."""
    if not current_version:
        return None
    from .versioning import semver_key

    current_key = semver_key(current_version)
    if current_key[0] < 0:
        return None
    count = 0
    seen_current = False
    for release in releases:
        version = release.display_version
        if not version:
            continue
        key = semver_key(version)
        if key == current_key:
            seen_current = True
            break
        if key > current_key:
            count += 1
    return count if seen_current else None


def _is_update_available(current_version: Optional[str], current_tag: Optional[str], latest: Release) -> Optional[bool]:
    if current_version and latest.display_version:
        return compare_versions(latest.display_version, current_version) > 0
    if current_tag and latest.tag:
        from .versioning import compare_tags

        return compare_tags(latest.tag, current_tag) > 0
    return None


def collect_issue_signal(
    cfg: Config,
    client: GitHubClient,
    release: Release,
    *,
    provenance: Optional[CodeProvenance] = None,
    logger: Optional[logging.Logger] = None,
    use_cache: bool = True,
) -> IssueSignal:
    """Search GitHub issues filed after the release, plus a baseline window."""
    log = logger or get_logger("issues")
    published = release.published_at
    if published is None:
        return IssueSignal(unavailable_reason="release has no publication timestamp")

    now = utcnow()
    window_days = max(0.5, hours_between(published, now) / 24.0)
    baseline_days = min(BASELINE_MAX_DAYS, max(BASELINE_MIN_DAYS, window_days))
    baseline_start = published - timedelta(days=baseline_days)

    signal = IssueSignal(window_days=window_days, baseline_window_days=baseline_days)

    post_query = build_issue_query(cfg.repo, created_after=published, keywords=ISSUE_KEYWORDS)
    post = client.search_issues(post_query, per_page=50, use_cache=use_cache)
    signal.queries.append(post_query)
    if not post.ok:
        signal.unavailable_reason = post.error or "issue search failed"
        signal.degraded = post.degraded
        return signal

    signal.post_release_total = post.total_count
    signal.degraded = signal.degraded or post.degraded
    items: list[Issue] = list(post.items)

    # Second query: bug-labelled issues (catches reports whose title lacks keywords).
    label_query = build_issue_query(cfg.repo, created_after=published, label="bug")
    label_result: IssueSearchResult = client.search_issues(label_query, per_page=50, use_cache=use_cache)
    signal.queries.append(label_query)
    if label_result.ok:
        signal.post_release_total = max(signal.post_release_total, label_result.total_count)
        items.extend(label_result.items)
        signal.degraded = signal.degraded or label_result.degraded

    # Baseline (same query shape, previous window) for normalisation.
    if cfg.check_issue_baseline:
        baseline_query = build_issue_query(
            cfg.repo,
            created_after=baseline_start,
            created_before=published,
            keywords=ISSUE_KEYWORDS,
        )
        baseline = client.search_issues(baseline_query, per_page=1, use_cache=use_cache)
        signal.queries.append(baseline_query)
        if baseline.ok:
            signal.baseline_total = baseline.total_count
        signal.degraded = signal.degraded or baseline.degraded

    # Deduplicate and classify (legacy keyword clusters).
    deduped: dict[int, Issue] = {issue.number: issue for issue in items if issue.number}
    signal.scanned_items = len(deduped)
    clusters = classify_issues(deduped.values())
    signal.category_counts = {key: len(value) for key, value in clusters.items()}
    signal.category_samples = clusters
    recent_cutoff = published + timedelta(hours=72)
    signal.open_severe_recent = sum(
        1
        for issues in clusters.values()
        for issue in issues
        if issue.state.lower() == "open" and issue.created_at is not None and issue.created_at <= recent_cutoff
    )

    # Graded regression clusters (severity x independence x corroboration).
    graded = build_clusters(
        deduped.values(),
        release_version=release.display_version,
        release_tag=release.tag,
    )
    limit = max(0, int(getattr(cfg.github, "issue_enrichment_limit", DEFAULT_ENRICHMENT_LIMIT)))
    if limit:
        targets = _enrichment_targets(graded, limit)
        if targets:
            enrichment = _enrich_issues(client, targets, logger=log, use_cache=use_cache)
            signal.enriched_issues = len(enrichment)
            if enrichment:
                graded = build_clusters(
                    deduped.values(),
                    enrichment=enrichment,
                    release_version=release.display_version,
                    release_tag=release.tag,
                )
    signal.clusters = graded
    signal.signal_confidence = issue_signal_confidence(signal)

    log.debug(
        "issues: post=%s baseline=%s scanned=%s clusters=%s confidence=%s",
        signal.post_release_total,
        signal.baseline_total,
        signal.scanned_items,
        {c.key: f"{c.reports}/{c.unique_reporters}" for c in graded},
        signal.signal_confidence,
    )
    return signal


def _enrichment_targets(clusters: list[RegressionCluster], limit: int) -> list[Issue]:
    """Which issues deserve a comments fetch (severe clusters, open first)."""
    pool: list[Issue] = []
    for cluster in clusters:
        if cluster.severity not in {SEVERITY_CRITICAL, SEVERITY_HIGH}:
            continue
        pool.extend(cluster.samples)
    pool.sort(key=lambda issue: (issue.state.lower() != "open", -issue.comments))
    seen: set[int] = set()
    targets: list[Issue] = []
    for issue in pool:
        if issue.number in seen:
            continue
        seen.add(issue.number)
        targets.append(issue)
        if len(targets) >= limit:
            break
    return targets


def _enrich_issues(
    client: GitHubClient,
    issues: list[Issue],
    *,
    logger: Any,
    use_cache: bool = True,
) -> dict[int, IssueEnrichment]:
    """Fetch comments for a bounded set of issues and grade the responses."""
    enrichment: dict[int, IssueEnrichment] = {}
    for issue in issues:
        try:
            comments = client.get_issue_comments(issue.number, use_cache=use_cache)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("comment fetch failed for #%s: %s", issue.number, exc)
            continue
        maintainer = [c for c in comments if c.from_maintainer]
        confirmed = any(_confirmation_language(c.body) for c in maintainer)
        linked_pr = any(re.search(r"#\d{3,6}", c.body or "") for c in maintainer) or bool(
            re.search(r"(fixed by|fixes|closes)\s+#\d{3,6}", issue.body or "", re.IGNORECASE)
        )
        enrichment[issue.number] = IssueEnrichment(
            number=issue.number,
            maintainer_reply=bool(maintainer),
            maintainer_confirmed=confirmed,
            linked_pr=linked_pr,
            reproduction=any(
                marker in (issue.body or "").lower()
                for marker in ("steps to reproduce", "to reproduce", "traceback", "error:")
            ),
            comments=len(comments),
            author_association=issue.author_association,
            notes=f"{len(maintainer)} maintainer comment(s)",
        )
    if enrichment:
        logger.debug("enriched %d issue(s) with comment data", len(enrichment))
    return enrichment


_CONFIRM_RE = re.compile(
    r"\b(confirm(ed|ing)?|reproduce[ds]?|repro\b|will fix|fixing|fixed in|on it|tracking|thanks for the report)\b",
    re.IGNORECASE,
)
_DENY_RE = re.compile(
    r"\b(cannot reproduce|can'?t reproduce|not reproducible|works for me|duplicate of)\b", re.IGNORECASE
)


def _confirmation_language(text: str) -> bool:
    if not text:
        return False
    if _DENY_RE.search(text):
        return False
    return bool(_CONFIRM_RE.search(text))


def issue_signal_confidence(signal: IssueSignal) -> int:
    """Issue Signal Confidence (0-100): how much should the issue data be trusted?

    ======================================================  ====
    Issue API answered                                        40
    Baseline window is usable (not flooded)                   20
    At least one cluster with two independent reporters       20  (a single-reporter cluster: 10)
    Comment enrichment succeeded                               10
    Data is not degraded (live, not stale cache)               10
    ======================================================  ====
    """
    if not signal.available:
        return 0
    score = 40
    if signal.baseline_total is not None and signal.baseline_total <= 1000:
        score += 20
    reporters = max((c.unique_reporters for c in signal.clusters), default=0)
    if reporters >= 2:
        score += 20
    elif reporters == 1:
        score += 10
    if signal.enriched_issues:
        score += 10
    if not signal.degraded:
        score += 10
    return int(max(0, min(100, score)))


def summarize_release_line(release: Release) -> str:
    """One-line label like ``v0.21.3 (v2026.9.14) - 0.6 days old``."""
    version = f"v{release.display_version}" if release.display_version else release.tag
    age = release.age_days
    age_text = f"{age:.1f}d" if age is not None else "?"
    return f"{version} ({release.tag}) - {age_text} old"


def level_of(score: Optional[int]) -> str:
    return level_for_score(score)


def iso_now() -> str:
    return iso(utcnow()) or ""


def normalise_release_tag(tag: Optional[str]) -> Optional[str]:
    return normalise_tag(tag)
