"""The risk engine: everything that turns observations into an Update Risk Score.

Design goals:

* **Explainable** - every point is attached to a named factor with a reason the
  user can read and argue with;
* **Conservative** - unknown data never lowers the score; it raises the
  uncertainty and can force an ``UNKNOWN`` verdict instead of a fake ``LOW``;
* **Tunable** - the five group caps live in the config file (``risk:`` section).

Groups (max points, defaults):

====================  ====  ==================================================
 group                pts   what it measures
====================  ====  ==================================================
 keyword_cap           40   *what changed* - release notes + commit subjects
 age_cap               18   *how young* the release is
 volume_cap            18   *how big* the diff is + release type (X.Y.0 ...)
 issues_cap            30   post-release issue signals (regressions, data loss)
 context_cap           12   install/context risk (main tracking, dirty tree...)
 bonus_cap            -15   stability bonus (matured release, quiet issues)
====================  ====  ==================================================

score = clamp(keyword + age + volume + issues + context + bonus, 0, 100)
stability = 100 - score
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .config import RiskWeights
from .github_api import Issue
from .util import clamp
from .clusters import (
    RegressionCluster,
    RegressionSignal,
    regression_signal as compute_regression_signal,
    UNKNOWN_REGRESSION_FLOOR,
)

# --------------------------------------------------------------------------- #
# levels
# --------------------------------------------------------------------------- #

LEVEL_LOW = "LOW"
LEVEL_LOW_MEDIUM = "LOW-MEDIUM"
LEVEL_MEDIUM = "MEDIUM"
LEVEL_HIGH = "HIGH"
LEVEL_VERY_HIGH = "VERY HIGH"
LEVEL_UNKNOWN = "UNKNOWN"

BAND_LOW_MAX = 20
BAND_LOW_MEDIUM_MAX = 40
BAND_MEDIUM_MAX = 60
BAND_HIGH_MAX = 80

RECOMMEND_UPDATE = "UPDATE"
RECOMMEND_WAIT = "WAIT"
RECOMMEND_AVOID = "AVOID"
RECOMMEND_UNKNOWN = "INSUFFICIENT_DATA"
RECOMMEND_UP_TO_DATE = "UP_TO_DATE"

RAWS_AGO_WINDOWS = (24, 72, 168, 336, 720)  # hours: 1d, 3d, 7d, 14d, 30d


def level_for_score(score: int | None) -> str:
    """Map a 0-100 score onto the documented bands."""
    if score is None:
        return LEVEL_UNKNOWN
    if score <= BAND_LOW_MAX:
        return LEVEL_LOW
    if score <= BAND_LOW_MEDIUM_MAX:
        return LEVEL_LOW_MEDIUM
    if score <= BAND_MEDIUM_MAX:
        return LEVEL_MEDIUM
    if score <= BAND_HIGH_MAX:
        return LEVEL_HIGH
    return LEVEL_VERY_HIGH


def is_severe_level(level: str) -> bool:
    return level in {LEVEL_HIGH, LEVEL_VERY_HIGH}


# --------------------------------------------------------------------------- #
# keyword model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class KeywordCategory:
    key: str
    weight: float
    zh: str
    en: str
    patterns: tuple[str, ...]

    def compiled(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.patterns]


KEYWORD_CATEGORIES: tuple[KeywordCategory, ...] = (
    KeywordCategory(
        key="breaking",
        weight=30,
        zh="Breaking Change / 不兼容变更",
        en="Breaking change",
        patterns=(
            r"\bbreaking[\s-]?changes?\b",
            r"\bbackwards?[\s-]incompatible\b",
            r"\bno longer (?:supported|supported|works?|available)\b",
            r"\bdeprecat(?:e|ed|ion)\b",
            r"\bremoved support\b",
        ),
    ),
    KeywordCategory(
        key="database",
        weight=25,
        zh="数据库 / 迁移 / schema",
        en="Database / migration / schema",
        patterns=(
            r"\bdatabase\b",
            r"\bmigrations?\b",
            r"\bschemas?\b",
            r"\bsqlite\b",
            r"\bstate\.db\b",
            r"\balembic\b",
            r"\bdb\b",
        ),
    ),
    KeywordCategory(
        key="session_state",
        weight=20,
        zh="Session / 状态存储",
        en="Session / state store",
        patterns=(
            r"\bsessions?\b",
            r"\bstate (?:store|file|db|machine)\b",
            r"\btranscripts?\b",
            r"\bstate\.db\b",
            r"\bcheckpoint\b",
        ),
    ),
    KeywordCategory(
        key="auth",
        weight=20,
        zh="认证 / 凭证 / Token",
        en="Auth / credentials / tokens",
        patterns=(
            r"\bauth(?:entication|orization|enticat\w*)?\b",
            r"\bcredentials?\b",
            r"\btokens?\b",
            r"\boauth\b",
            r"\bsecrets?\b",
            r"\bapi keys?\b",
        ),
    ),
    KeywordCategory(
        key="rewrite",
        weight=20,
        zh="大规模重写 / 重构",
        en="Rewrite / refactor",
        patterns=(
            r"\brewrit(?:e|es|ten|ing)\b",
            r"\brefactors?\b",
            r"\brefactor(?:ed|ing)\b",
            r"\boverhauls?\b",
            r"\brearchitect(?:s|ed|ing)?\b",
            r"\brework(?:s|ed|ing)?\b",
            r"\brevamps?\b",
        ),
    ),
    KeywordCategory(
        key="gateway",
        weight=15,
        zh="Gateway",
        en="Gateway",
        patterns=(r"\bgateways?\b",),
    ),
    KeywordCategory(
        key="config",
        weight=15,
        zh="配置 / 配置迁移",
        en="Config / config migration",
        patterns=(
            r"\bconfigs?\b",
            r"\bconfigurations?\b",
            r"\bconfig(?:uration)?\s+migration\b",
            r"\bya?ml\b",
        ),
    ),
    KeywordCategory(
        key="storage_backend",
        weight=10,
        zh="存储 / 序列化 / 后端",
        en="Storage / serialization / backend",
        patterns=(
            r"\bstorages?\b",
            r"\bserializ(?:e|ation|ing)\b",
            r"\bbackends?\b",
            r"\bmemory?\b",
            r"\bpersist(?:ence|ed|ing)?\b",
        ),
    ),
    KeywordCategory(
        key="provider_sdk",
        weight=8,
        zh="Provider / SDK / MCP / 工具系统",
        en="Provider / SDK / MCP / tool system",
        patterns=(
            r"\bproviders?\b",
            r"\bsdk\b",
            r"\bmcp\b",
            r"\btool ?sets?\b",
            r"\btool system\b",
            r"\bapi\b",
        ),
    ),
)

#: These lower the score: they describe changes that historically do not break installs.
LOW_RISK_PATTERNS: tuple[str, ...] = (
    r"\bdocs?\b",
    r"\bdocumentation\b",
    r"\btypos?\b",
    r"\bcosmetic\b",
    r"\bui\b",
    r"\bstyle\b",
    r"\bskins?\b",
    r"\bthemes?\b",
    r"\breadme\b",
    r"\btranslations?\b",
    r"\bchangelog\b",
    r"\bminor fixes?\b",
    r"\bmodel support\b",
    r"\bnew provider\b",
)

#: Sum of all category weights - the normaliser for the keyword group.
_TOTAL_KEYWORD_WEIGHT = sum(cat.weight for cat in KEYWORD_CATEGORIES)

_LOW_RISK_COMPILED = [re.compile(p, re.IGNORECASE) for p in LOW_RISK_PATTERNS]
_CATEGORY_COMPILED: dict[str, list[re.Pattern[str]]] = {cat.key: cat.compiled() for cat in KEYWORD_CATEGORIES}


@dataclass
class KeywordHit:
    key: str
    label_zh: str
    label_en: str
    weight: float
    hits: int
    samples: list[str] = field(default_factory=list)

    @property
    def intensity(self) -> float:
        """Diminishing-returns intensity: 1 hit -> 0.55, 8+ hits -> 1.0."""
        if self.hits <= 0:
            return 0.0
        return min(1.0, 0.55 + 0.15 * math.log2(self.hits))


@dataclass
class KeywordScan:
    corpus_chars: int = 0
    corpus_lines: int = 0
    hits: list[KeywordHit] = field(default_factory=list)
    low_risk_hits: int = 0
    low_risk_samples: list[str] = field(default_factory=list)

    @property
    def raw_points(self) -> float:
        return sum(hit.weight * hit.intensity for hit in self.hits)

    @property
    def matched_categories(self) -> int:
        return sum(1 for hit in self.hits if hit.hits > 0)

    @property
    def total_hits(self) -> int:
        return sum(hit.hits for hit in self.hits)


def scan_keywords(text: str | None, *, extra: Sequence[str] = ()) -> KeywordScan:
    """Scan release notes / commit subjects for risk and low-risk language."""
    corpus = text or ""
    if extra:
        corpus = corpus + "\n" + "\n".join(extra)
    scan = KeywordScan(corpus_chars=len(corpus), corpus_lines=max(1, corpus.count("\n") + 1))
    if not corpus.strip():
        return scan

    for category in KEYWORD_CATEGORIES:
        patterns = _CATEGORY_COMPILED[category.key]
        hits = 0
        samples: list[str] = []
        for pattern in patterns:
            for match in pattern.finditer(corpus):
                hits += 1
                if len(samples) < 3:
                    samples.append(_context(corpus, match.start(), match.end()))
        if hits:
            scan.hits.append(
                KeywordHit(
                    key=category.key,
                    label_zh=category.zh,
                    label_en=category.en,
                    weight=category.weight,
                    hits=hits,
                    samples=samples,
                )
            )

    for pattern in _LOW_RISK_COMPILED:
        for match in pattern.finditer(corpus):
            scan.low_risk_hits += 1
            if len(scan.low_risk_samples) < 3:
                scan.low_risk_samples.append(_context(corpus, match.start(), match.end()))
    return scan


def _context(text: str, start: int, end: int, width: int = 48) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    snippet = text[left:right].replace("\n", " ").strip()
    prefix = "..." if left > 0 else ""
    suffix = "..." if right < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


# --------------------------------------------------------------------------- #
# factor helpers (each one is unit-tested in isolation)
# --------------------------------------------------------------------------- #


def keyword_points(scan: KeywordScan, cap: float) -> float:
    """Scale the weighted keyword hits into the keyword group cap."""
    if scan.raw_points <= 0:
        return 0.0
    return cap * min(1.0, scan.raw_points / _TOTAL_KEYWORD_WEIGHT)


def age_points(age_hours: Optional[float], cap: float) -> tuple[float, Optional[str]]:
    """Release-age contribution; very matured releases earn a small bonus."""
    if age_hours is None:
        return cap * 0.5, "age unknown"
    if age_hours < 0:
        age_hours = 0.0
    if age_hours < 24:
        return cap, "less than 24 h old"
    if age_hours < 72:
        return cap * (13 / 18), "1-3 days old (observation window)"
    if age_hours < 168:
        return cap * (8 / 18), "3-7 days old"
    if age_hours < 336:
        return cap * (3 / 18), "7-14 days old"
    if age_hours < 720:
        return 0.0, "14-30 days old"
    return -(cap * (5 / 18)), "more than 30 days old (matured)"


def commit_volume_points(total_commits: Optional[int], cap: float) -> tuple[float, Optional[str]]:
    """Diff-size contribution from the commit count."""
    if total_commits is None:
        return cap * (2 / 18), "commit count unknown"
    if total_commits < 10:
        return 0.0, None
    if total_commits < 50:
        return cap * (2 / 18), "small diff"
    if total_commits < 200:
        return cap * (5 / 18), "moderate diff"
    if total_commits < 500:
        return cap * (9 / 18), "large diff"
    if total_commits < 1500:
        return cap * (13 / 18), "very large diff"
    return cap * (16 / 18), "extremely large diff"


def pr_volume_points(pr_count: Optional[int], cap: float) -> float:
    if pr_count is None:
        return 0.0
    if pr_count < 5:
        return 0.0
    if pr_count < 20:
        return cap * (2 / 18)
    if pr_count < 100:
        return cap * (5 / 18)
    if pr_count < 300:
        return cap * (8 / 18)
    return cap * (11 / 18)


def release_type_points(
    *, latest_version: Optional[str], current_version: Optional[str], cap: float
) -> tuple[float, Optional[str]]:
    """X.Y.0 feature releases and major bumps are treated more carefully than patches."""
    from .versioning import classify_bump, is_feature_release

    bump = classify_bump(current_version, latest_version)
    if bump == "major":
        return cap * (6 / 18), "major version bump"
    if is_feature_release(latest_version):
        return cap * (4 / 18), "feature release (X.Y.0)"
    return 0.0, None


def rollup_points(release_body: str | None, total_commits: Optional[int], cap: float) -> tuple[float, Optional[str]]:
    """A 'patch' that rolls up hundreds of PRs is a feature release in disguise."""
    text = (release_body or "").lower()
    hint = any(token in text for token in ("rolls up", "rollup", "roll-up", "roll up", "unreleased changes"))
    if hint or (total_commits is not None and total_commits >= 300):
        return cap * (3 / 18), "patch release bundling a large number of merged PRs"
    return 0.0, None


#: Issue clusters that indicate a *severe* regression class.
SEVERITY_CLUSTERS: dict[str, dict[str, object]] = {
    "data_loss": {
        "zh": "数据损坏 / Session 丢失",
        "en": "data corruption / session loss",
        "patterns": (r"state\.db", r"\bcorrupt", r"session.{0,20}(?:lost|lost|missing|gone)", r"data loss", r"\bdatabase\b"),
    },
    "upgrade_failure": {
        "zh": "升级失败 / 回滚问题",
        "en": "update or upgrade failure",
        "patterns": (r"\bupgrad", r"update fail", r"can'?t update", r"fail(?:s|ed)? to (?:update|install)", r"\brollback\b"),
    },
    "gateway_down": {
        "zh": "Gateway 无法启动 / 异常",
        "en": "gateway broken or down",
        # A bare "gateway" mention is not a regression report - require failure language.
        "patterns": (
            r"gateway[^.]{0,60}\b(?:fail|crash|broken|down|not running|won'?t|can'?t|refuse|deadlock|live-?lock|hang|loop|dies?|stuck|restart)",
            r"\b(?:fail|crash|broken|down|won'?t start|can'?t start)[^.]{0,40}gateway",
        ),
    },
    "config_migration": {
        "zh": "配置迁移异常",
        "en": "config migration problems",
        "patterns": (r"config.{0,20}(?:migrat|broken|invalid)", r"\bmigrat(?:e|ion)\b.{0,20}config"),
    },
}

#: Above this many matching issues the search API's own counts stop being a
#: reliable basis for a rate comparison (very high-traffic repositories).
UNRELIABLE_ISSUE_COUNT = 1000


@dataclass
class IssueSignal:
    """Aggregated post-release issue evidence (see ``checker.collect_issue_signal``)."""

    window_days: float = 0.0
    post_release_total: int = 0
    baseline_total: Optional[int] = None
    baseline_window_days: Optional[float] = None
    scanned_items: int = 0
    category_counts: dict[str, int] = field(default_factory=dict)
    category_samples: dict[str, list[Issue]] = field(default_factory=dict)
    open_severe_recent: int = 0
    queries: list[str] = field(default_factory=list)
    degraded: bool = False
    unavailable_reason: Optional[str] = None
    #: graded regression clusters (clusters.build_clusters) and their credibility
    clusters: list[RegressionCluster] = field(default_factory=list)
    signal_confidence: Optional[int] = None
    enriched_issues: int = 0

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    def to_dict(self) -> dict[str, object]:
        return {
            "window_days": round(self.window_days, 2),
            "post_release_total": self.post_release_total,
            "baseline_total": self.baseline_total,
            "baseline_window_days": self.baseline_window_days,
            "scanned_items": self.scanned_items,
            "category_counts": self.category_counts,
            "open_severe_recent": self.open_severe_recent,
            "degraded": self.degraded,
            "unavailable_reason": self.unavailable_reason,
            "queries": self.queries,
            "signal_confidence": self.signal_confidence,
            "enriched_issues": self.enriched_issues,
            "clusters": [c.to_dict() for c in self.clusters],
            "samples": {
                key: [i.to_dict() for i in issues[:3]] for key, issues in self.category_samples.items()
            },
        }


def classify_issues(issues: Iterable[Issue]) -> dict[str, list[Issue]]:
    """Group issues into severe regression clusters by title/label text."""
    clusters: dict[str, list[Issue]] = {key: [] for key in SEVERITY_CLUSTERS}
    compiled = {key: [re.compile(p, re.IGNORECASE) for p in spec["patterns"]] for key, spec in SEVERITY_CLUSTERS.items()}
    for issue in issues:
        haystack = f"{issue.title} {issue.label_text}"
        for key, patterns in compiled.items():
            if any(p.search(haystack) for p in patterns):
                clusters[key].append(issue)
    return {key: value for key, value in clusters.items() if value}


def issue_volume_points(signal: Optional[IssueSignal], cap: float) -> tuple[float, list[tuple[str, str]]]:
    """The *quantitative* half of the issue analysis: how much noise is there?

    Kept separate from :func:`issue_points` because the graded Regression Signal
    folds this in at half weight (counts are the weakest evidence we have), while
    the legacy additive view uses the full value.
    """
    if signal is None or not signal.available:
        return cap * 0.5, [
            (
                "Issue 数据不可用（按半个上限计入，缺失不等于零风险）",
                "issue data unavailable (counted as half the cap; missing is not zero risk)",
            )
        ]

    reasons: list[tuple[str, str]] = []
    window = max(0.25, signal.window_days or 1.0)
    post_rate = signal.post_release_total / window

    if signal.post_release_total > UNRELIABLE_ISSUE_COUNT:
        reasons.append(
            (
                f"发布后命中 {signal.post_release_total} 个 Issue（绝对数量极大）",
                f"{signal.post_release_total} matching issues since the release (very large absolute volume)",
            )
        )
        return 16.0, reasons
    if signal.baseline_total is not None and signal.baseline_total > UNRELIABLE_ISSUE_COUNT:
        # e.g. 28k issues filed in 3 days: the API's counts are not a usable
        # baseline any more, so lean on the severity clusters instead.
        reasons.append(
            (
                f"基线窗口 Issue 数量过大（{signal.baseline_total}），速率对比不可靠；以严重问题报告为准",
                f"baseline issue volume is too large ({signal.baseline_total}) for a reliable rate comparison; "
                "relying on the severity reports instead",
            )
        )
        return 4.0, reasons
    if signal.baseline_total is not None and signal.baseline_window_days:
        baseline_rate = signal.baseline_total / max(0.25, signal.baseline_window_days)
        ratio = post_rate / max(baseline_rate, 0.5)
        if ratio <= 0.5:
            reasons.append(
                (
                    f"Issue 速率低于仓库基线（比值 {ratio:.2f}）",
                    f"issue rate below the repo baseline (ratio {ratio:.2f})",
                )
            )
            return 0.0, reasons
        if ratio <= 1.5:
            reasons.append(
                (
                    f"Issue 速率与仓库基线相当（比值 {ratio:.2f}）",
                    f"issue rate in line with the repo baseline (ratio {ratio:.2f})",
                )
            )
            return 4.0, reasons
        if ratio <= 3:
            reasons.append((f"Issue 速率为基线的 {ratio:.1f} 倍", f"issue rate {ratio:.1f}x above baseline"))
            return 8.0, reasons
        if ratio <= 6:
            reasons.append((f"Issue 速率为基线的 {ratio:.1f} 倍", f"issue rate {ratio:.1f}x above baseline"))
            return 13.0, reasons
        reasons.append(
            (
                f"Issue 速率为基线的 {ratio:.1f} 倍（强烈提示回归潮）",
                f"issue rate {ratio:.1f}x above baseline (strong signal of a regression wave)",
            )
        )
        return 18.0, reasons

    # No baseline: fall back to absolute counts, deliberately conservative.
    count = signal.post_release_total
    if count == 0:
        return 0.0, reasons
    if count <= 2:
        reasons.append((f"发布后 {count} 个疑似问题", f"{count} possible issue(s) since the release"))
        return 4.0, reasons
    if count <= 5:
        reasons.append((f"发布后 {count} 个疑似问题", f"{count} possible issue(s) since the release"))
        return 8.0, reasons
    if count <= 15:
        reasons.append((f"发布后 {count} 个疑似问题", f"{count} possible issue(s) since the release"))
        return 13.0, reasons
    reasons.append(
        (
            f"发布后 {count} 个疑似问题（无基线可比）",
            f"{count} possible issues since the release (no baseline available)",
        )
    )
    return 18.0, reasons


def issue_points(signal: Optional[IssueSignal], cap: float) -> tuple[float, list[tuple[str, str]], Optional[str]]:
    """Post-release issue contribution (legacy additive view).

    Returns ``(points, reasons, unknown_reason)`` where each reason is a
    ``(中文, English)`` pair. Missing data is *not* free: it contributes half the
    cap and is reported as unknown.
    """
    if signal is None or not signal.available:
        reason = (signal.unavailable_reason if signal else None) or "issue data unavailable"
        return cap * 0.5, [], reason

    volume, reasons = issue_volume_points(signal, cap)
    window = max(0.25, signal.window_days or 1.0)
    post_rate = signal.post_release_total / window
    _ = post_rate  # kept for parity with the documented formula

    severity = 0.0
    for key, count in sorted(signal.category_counts.items(), key=lambda kv: -kv[1]):
        label_zh = str(SEVERITY_CLUSTERS.get(key, {}).get("zh", key))
        label_en = str(SEVERITY_CLUSTERS.get(key, {}).get("en", key))
        if count >= 2:
            severity += 6.0
            reasons.append(
                (
                    f"多个独立报告：{label_zh}（{count} 个）",
                    f"multiple independent reports: {label_en} ({count})",
                )
            )
        elif count == 1:
            severity += 3.0
            reasons.append((f"单个报告：{label_zh}", f"single report: {label_en}"))
    severity = min(12.0, severity)

    open_bonus = 3.0 if signal.open_severe_recent >= 1 else 0.0
    if open_bonus:
        reasons.append(
            (
                "发布后 72 小时内有仍处于 open 的严重问题",
                "open severe issue filed within 72 h of the release",
            )
        )

    return min(cap, volume + severity + open_bonus), reasons, None


def context_points(
    *,
    tracking_main: bool,
    dirty_tree: bool,
    install_kind: str,
    target_prerelease: bool,
    cap: float,
) -> tuple[float, list[str]]:
    points = 0.0
    reasons: list[str] = []
    if tracking_main:
        points += cap * (5 / 12)
        reasons.append("install tracks the main branch")
    if target_prerelease:
        points += cap * (6 / 12)
        reasons.append("target release is a prerelease")
    if dirty_tree:
        points += cap * (3 / 12)
        reasons.append("git working tree has uncommitted changes")
    if install_kind == "unknown":
        points += cap * (4 / 12)
        reasons.append("install method could not be determined")
    elif install_kind in {"docker", "nix"}:
        points += cap * (2 / 12)
        reasons.append(f"install method is {install_kind} (update path is less predictable)")
    return min(cap, points), reasons


def bonus_points(
    *,
    age_hours: Optional[float],
    scan: KeywordScan,
    signal: Optional[IssueSignal],
    total_commits: Optional[int],
    cap: float,
) -> tuple[float, list[str]]:
    """Stability bonuses (negative points), capped."""
    bonus = 0.0
    reasons: list[str] = []
    if age_hours is not None and age_hours >= 720:
        bonus -= cap * (5 / 15)
        reasons.append("release matured for more than 30 days")
    if scan.total_hits == 0 and scan.low_risk_hits > 0:
        bonus -= cap * (7 / 15)
        reasons.append("release notes are documentation/UI-only")
    if signal is not None and signal.available and signal.post_release_total == 0 and (age_hours or 0) >= 72:
        bonus -= cap * (4 / 15)
        reasons.append("no risk-keyword issues filed since the release")
    if total_commits is not None and total_commits < 10 and (age_hours or 0) >= 168:
        bonus -= cap * (3 / 15)
        reasons.append("tiny diff and more than a week of soak time")
    return max(-cap, bonus), reasons


# --------------------------------------------------------------------------- #
# assessment
# --------------------------------------------------------------------------- #


@dataclass
class RiskFactor:
    key: str
    label_zh: str
    label_en: str
    points: float
    detail_zh: str = ""
    detail_en: str = ""
    unknown: bool = False

    @property
    def label(self) -> str:
        return self.label_zh

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label_zh": self.label_zh,
            "label_en": self.label_en,
            "points": round(self.points, 2),
            "detail_zh": self.detail_zh,
            "detail_en": self.detail_en,
            "unknown": self.unknown,
        }


@dataclass
class CheckContext:
    """Everything the risk engine is allowed to look at (fully mockable)."""

    # local
    local_version: Optional[str] = None
    local_tag: Optional[str] = None
    tracking_main: bool = False
    install_kind: str = "unknown"
    dirty_tree: bool = False
    # target release
    latest_version: Optional[str] = None
    latest_tag: Optional[str] = None
    latest_prerelease: bool = False
    release_age_hours: Optional[float] = None
    release_body: str = ""
    release_name: str = ""
    # diff
    total_commits: Optional[int] = None
    commit_count_known: bool = True
    pr_count: Optional[int] = None
    versions_behind: Optional[int] = None
    commit_subjects: list[str] = field(default_factory=list)
    # issues
    issues: Optional[IssueSignal] = None
    issues_enabled: bool = True
    # provenance / data completeness (feed Data Confidence)
    provenance_ok: bool = True
    provenance_channel: str = ""
    channel_matches_preferred: bool = True
    # config
    weights: RiskWeights = field(default_factory=RiskWeights)
    risk_threshold: int = 40
    minimum_release_age_days: float = 5.0
    # housekeeping
    degraded_inputs: list[str] = field(default_factory=list)


@dataclass
class RiskAssessment:
    """The graded result.

    ``score`` / ``level`` / ``stability`` are the **overall** values (kept for
    backwards compatibility); the new fields split that number into its parts so
    a user can see *why* the verdict came out the way it did:

    * ``change_risk``      - the update itself (notes, age, diff size)
    * ``regression_signal``- problems other people already hit
    * ``data_confidence``  - how complete the observation is
    """

    score: Optional[int]
    level: str
    stability: Optional[int]
    recommendation: str
    confidence: float
    factors: list[RiskFactor] = field(default_factory=list)
    unknown_areas: list[str] = field(default_factory=list)
    summary_zh: list[str] = field(default_factory=list)
    summary_en: list[str] = field(default_factory=list)
    recheck_in_days: Optional[int] = None
    age_days: Optional[float] = None
    insufficient_data: bool = False
    # -- new component view -------------------------------------------------- #
    change_risk: Optional[int] = None
    regression_signal: Optional[int] = None
    data_confidence: Optional[int] = None
    environment_risk: Optional[int] = None
    overall: Optional[int] = None
    score_is_lower_bound: bool = False
    regression_detail: Optional[RegressionSignal] = None

    @property
    def score_or_unknown(self) -> str:
        if self.score is None:
            return "UNKNOWN"
        if self.score >= 100:
            return "100 (lower bound)" if self.score_is_lower_bound else "100/100"
        return f">= {self.score}" if self.score_is_lower_bound else f"{self.score}/100"

    @property
    def stability_or_unknown(self) -> str:
        return f"{self.stability}/100" if self.stability is not None else "UNKNOWN"

    def component_rows(self) -> list[tuple[str, str]]:
        """(label, value) pairs for the RISK section of the report."""
        rows = [
            ("Change Risk", _fmt_component(self.change_risk)),
            ("Regression Signal", _fmt_component(self.regression_signal, lower_bound=self.score_is_lower_bound)),
            ("Data Confidence", _fmt_component(self.data_confidence)),
        ]
        if self.environment_risk is not None:
            rows.append(("Environment Risk", _fmt_component(self.environment_risk)))
        rows.append(("Overall Risk", self.score_or_unknown))
        rows.append(("Risk Level", self.level))
        rows.append(("Stability", self.stability_or_unknown))
        return rows

    def to_dict(self) -> dict[str, object]:
        return {
            "score": self.score,
            "level": self.level,
            "stability": self.stability,
            "recommendation": self.recommendation,
            "confidence": round(self.confidence, 3),
            "recheck_in_days": self.recheck_in_days,
            "age_days": round(self.age_days, 2) if self.age_days is not None else None,
            "insufficient_data": self.insufficient_data,
            "unknown_areas": self.unknown_areas,
            "factors": [f.to_dict() for f in self.factors],
            # new component view
            "change_risk": self.change_risk,
            "regression_signal": self.regression_signal,
            "data_confidence": self.data_confidence,
            "environment_risk": self.environment_risk,
            "overall": self.overall if self.overall is not None else self.score,
            "score_is_lower_bound": self.score_is_lower_bound,
            "regression_detail": self.regression_detail.to_dict() if self.regression_detail else None,
        }


def _fmt_component(value: Optional[int], *, lower_bound: bool = False) -> str:
    if value is None:
        return "UNKNOWN"
    return f">= {value} / 100" if lower_bound else f"{value} / 100"


def _clip_reasons(reasons: list[str], limit: int) -> list[str]:
    """Keep the factor table readable: at most ``limit`` items plus a count."""
    if len(reasons) <= limit:
        return reasons
    return reasons[:limit] + [f"… (+{len(reasons) - limit})"]


def assess(ctx: CheckContext) -> RiskAssessment:
    """Run every factor and combine them into the final assessment."""
    weights = ctx.weights
    factors: list[RiskFactor] = []
    unknown_areas: list[str] = []
    summary_zh: list[str] = []
    summary_en: list[str] = []

    # 1. keywords ---------------------------------------------------------- #
    scan = scan_keywords(ctx.release_body, extra=ctx.commit_subjects)
    kw_points = keyword_points(scan, weights.keyword_cap)
    kw_detail_zh = ", ".join(f"{hit.label_zh}({hit.hits})" for hit in sorted(scan.hits, key=lambda h: -h.weight)) or "无"
    kw_detail_en = ", ".join(f"{hit.label_en}({hit.hits})" for hit in sorted(scan.hits, key=lambda h: -h.weight)) or "none"
    factors.append(
        RiskFactor(
            key="keywords",
            label_zh="变更内容风险（Release Notes / commit）",
            label_en="Changed-area risk (release notes / commits)",
            points=kw_points,
            detail_zh=kw_detail_zh,
            detail_en=kw_detail_en,
        )
    )
    for hit in sorted(scan.hits, key=lambda h: -h.weight * h.intensity):
        if hit.weight >= 15:
            summary_zh.append(f"涉及 {hit.label_zh}（命中 {hit.hits} 次）")
            summary_en.append(f"{hit.label_en} touched ({hit.hits} mentions)")
    if scan.low_risk_hits and not scan.total_hits:
        summary_zh.append("变更以文档/UI 为主")
        summary_en.append("changes look documentation/UI only")

    # 2. age ---------------------------------------------------------------- #
    age_points_value, age_reason = age_points(ctx.release_age_hours, weights.age_cap)
    factors.append(
        RiskFactor(
            key="age",
            label_zh="发布年龄",
            label_en="Release age",
            points=age_points_value,
            detail_zh=age_reason or "已充分沉淀",
            detail_en=age_reason or "well matured",
            unknown=ctx.release_age_hours is None,
        )
    )
    if ctx.release_age_hours is None:
        unknown_areas.append("release_date")
        summary_zh.append("无法确定发布时间")
        summary_en.append("release date unknown")
    elif ctx.release_age_hours < 24:
        summary_zh.append("新版本发布不足 24 小时")
        summary_en.append("release is less than 24 hours old")

    # 3. volume + type ------------------------------------------------------ #
    volume_commit, volume_reason = commit_volume_points(ctx.total_commits, weights.volume_cap)
    volume_pr = pr_volume_points(ctx.pr_count, weights.volume_cap)
    type_points, type_reason = release_type_points(
        latest_version=ctx.latest_version, current_version=ctx.local_version, cap=weights.volume_cap
    )
    rollup, rollup_reason = rollup_points(ctx.release_body, ctx.total_commits, weights.volume_cap)
    volume_total = min(weights.volume_cap, max(volume_commit, volume_pr) + type_points + rollup)
    volume_details = [r for r in (volume_reason, type_reason, rollup_reason) if r]
    factors.append(
        RiskFactor(
            key="volume",
            label_zh="改动规模与版本类型",
            label_en="Diff size and release type",
            points=volume_total,
            detail_zh="；".join(volume_details) or "小幅改动",
            detail_en="; ".join(volume_details) or "small change",
        )
    )
    if ctx.total_commits is None or not ctx.commit_count_known:
        unknown_areas.append("commit_count")
        summary_zh.append("无法确定 commit 数量")
        summary_en.append("commit count unknown")
    elif ctx.total_commits >= 300:
        summary_zh.append(f"本次发布包含大量 commit（{ctx.total_commits}+）")
        summary_en.append(f"release contains a large number of commits ({ctx.total_commits}+)")
    if type_reason:
        summary_zh.append(type_reason)
        summary_en.append(type_reason)

    # 4. issues ------------------------------------------------------------- #
    issues_pts, issue_reasons, issues_unknown = issue_points(ctx.issues, weights.issues_cap)
    # each reason is a (中文, English) pair
    issue_reasons_zh = [zh for zh, _en in issue_reasons]
    issue_reasons_en = [en for _zh, en in issue_reasons]
    factors.append(
        RiskFactor(
            key="issues",
            label_zh="发布后 Issue 信号",
            label_en="Post-release issue signals",
            points=issues_pts,
            detail_zh="；".join(issue_reasons_zh) or "无异常信号",
            detail_en="; ".join(issue_reasons_en) or "no signals",
            unknown=issues_unknown is not None,
        )
    )
    if issues_unknown:
        unknown_areas.append("github_issues")
        summary_zh.append("GitHub Issues 数据不足（未取得，不能视为安全）")
        summary_en.append("GitHub issue data unavailable (not the same as safe)")
    summary_zh.extend(issue_reasons_zh[:4])
    summary_en.extend(issue_reasons_en[:4])

    # 5. context ------------------------------------------------------------ #
    ctx_points, ctx_reasons = context_points(
        tracking_main=ctx.tracking_main,
        dirty_tree=ctx.dirty_tree,
        install_kind=ctx.install_kind,
        target_prerelease=ctx.latest_prerelease,
        cap=weights.context_cap,
    )
    factors.append(
        RiskFactor(
            key="context",
            label_zh="安装方式与本地上下文",
            label_en="Install method and local context",
            points=ctx_points,
            detail_zh="；".join(ctx_reasons) or "无额外风险",
            detail_en="; ".join(ctx_reasons) or "no extra risk",
        )
    )
    if ctx.tracking_main:
        summary_zh.append("当前安装跟踪 main 分支，稳定性可能低于正式 Release")
        summary_en.append("install tracks main: less stable than a tagged release")

    # 6. bonus -------------------------------------------------------------- #
    bonus, bonus_reasons = bonus_points(
        age_hours=ctx.release_age_hours,
        scan=scan,
        signal=ctx.issues,
        total_commits=ctx.total_commits,
        cap=weights.bonus_cap,
    )
    factors.append(
        RiskFactor(
            key="bonus",
            label_zh="稳定性加分（负分）",
            label_en="Stability bonus (negative points)",
            points=bonus,
            detail_zh="；".join(bonus_reasons) or "无",
            detail_en="; ".join(bonus_reasons) or "none",
        )
    )

    # 6b. graded regression clusters --------------------------------------- #
    clusters_list = list(ctx.issues.clusters) if (ctx.issues is not None and ctx.issues.available) else []
    volume_only_points, _ = issue_volume_points(ctx.issues, weights.issues_cap)
    volume_ratio = min(1.0, volume_only_points / weights.issues_cap) if weights.issues_cap else 0.0
    regression_unknown = ctx.issues is None or not ctx.issues.available
    regression = compute_regression_signal(
        clusters_list,
        volume_ratio=volume_ratio,
        signal_confidence=(ctx.issues.signal_confidence if ctx.issues is not None else None),
        unavailable=regression_unknown,
        unavailable_reason=(
            (ctx.issues.unavailable_reason if ctx.issues is not None else None)
            or ("issue analysis disabled" if not ctx.issues_enabled else "issue data unavailable")
        ),
    )
    regression_value = regression.value if regression.value is not None else regression.floor
    cluster_detail_zh = "；".join(_clip_reasons(regression.reasons_zh, 4)) or "无回归聚类"
    cluster_detail_en = "; ".join(_clip_reasons(regression.reasons_en, 4)) or "no regression clusters"
    factors.append(
        RiskFactor(
            key="regression_clusters",
            label_zh="回归聚类可信度（Regression Signal）",
            label_en="Regression cluster credibility",
            points=weights.issues_cap * (regression_value / 100.0),
            detail_zh=cluster_detail_zh,
            detail_en=cluster_detail_en,
            unknown=regression.value is None,
        )
    )
    for cluster in clusters_list[:4]:
        summary_zh.append(cluster.headline_zh())
        summary_en.append(cluster.headline_en())
    if regression.value is None:
        summary_zh.append(
            f"回归信号无法评估（按 >= {regression.floor} 处理，缺失证据不等于零风险）"
        )
        summary_en.append(
            f"regression signal cannot be measured (assumed >= {regression.floor}; missing evidence is not zero risk)"
        )

    # ---- combine ---------------------------------------------------------- #
    change_denominator = max(1.0, weights.keyword_cap + weights.age_cap + weights.volume_cap)
    change_points_total = kw_points + age_points_value + volume_total
    change_risk = int(round(100.0 * clamp(change_points_total / change_denominator, 0.0, 1.0)))
    environment_risk = int(round(100.0 * clamp(ctx_points / max(1.0, weights.context_cap), 0.0, 1.0)))
    regression_ratio = clamp(regression_value / 100.0, 0.0, 1.0)

    # Risk components combine like independent failure classes (probabilistic OR),
    # so a bad release cannot hide behind a quiet one, then the stability bonus
    # subtracts directly.
    combined = 1.0 - (
        (1.0 - change_risk / 100.0) * (1.0 - regression_ratio) * (1.0 - environment_risk / 100.0)
    )
    combined -= abs(bonus) / 100.0
    score = int(round(100.0 * clamp(combined, 0.0, 1.0)))

    data_confidence = _data_confidence(ctx, scan)
    confidence = round(data_confidence / 100.0, 3)
    insufficient = _insufficient(ctx, confidence)
    observation_incomplete = bool(
        (ctx.issues_enabled and (ctx.issues is None or not ctx.issues.available))
        or not ctx.commit_count_known
        or (ctx.issues is not None and ctx.issues.degraded)
    )

    level = LEVEL_UNKNOWN if insufficient else level_for_score(score)
    recommendation, recheck = _recommendation(
        score=risk_threshold_check(score, ctx),
        level=level,
        insufficient=insufficient,
        age_days=(ctx.release_age_hours / 24.0) if ctx.release_age_hours is not None else None,
        threshold=ctx.risk_threshold,
        min_age_days=ctx.minimum_release_age_days,
    )

    return RiskAssessment(
        score=None if insufficient else score,
        level=level,
        stability=None if insufficient else 100 - score,
        recommendation=recommendation,
        confidence=confidence,
        factors=factors,
        unknown_areas=unknown_areas,
        summary_zh=summary_zh,
        summary_en=summary_en,
        recheck_in_days=recheck,
        age_days=(ctx.release_age_hours / 24.0) if ctx.release_age_hours is not None else None,
        insufficient_data=insufficient,
        change_risk=change_risk,
        regression_signal=regression.value,
        data_confidence=data_confidence,
        environment_risk=environment_risk,
        overall=None if insufficient else score,
        score_is_lower_bound=observation_incomplete and not insufficient,
        regression_detail=regression,
    )


def risk_threshold_check(score: int, ctx: CheckContext) -> int:
    """Applied risk used for the recommendation (currently identical to the score).

    Kept as a named function so threshold policies (e.g. stricter gate while
    tracking main) have a single place to live.
    """
    return score


def _confidence(ctx: CheckContext, scan: KeywordScan) -> float:
    """Backwards-compatible 0..1 view of :func:`_data_confidence`."""
    return round(_data_confidence(ctx, scan) / 100.0, 3)


def _data_confidence(ctx: CheckContext, scan: KeywordScan) -> int:
    """Data Confidence (0-100): how complete is the picture we judged from?

    Checklist (documented, auditable):

    ==============================  ====
    Release API gave us a release     25
    Compare API gave us a diff size   20
    Issue API answered                20  (15 when it only answered from cache)
    Git provenance is usable          20
    Release notes are readable        15
    ==============================  ====
    """
    score = 0
    if ctx.latest_version or ctx.latest_tag:
        score += 25
    if ctx.commit_count_known and ctx.total_commits is not None:
        score += 20
    if ctx.issues is not None and ctx.issues.available:
        score += 15 if ctx.issues.degraded else 20
    if ctx.provenance_ok:
        score += 20
    if len((ctx.release_body or "").strip()) >= 200:
        score += 15
    return int(max(0, min(100, score)))


def _insufficient(ctx: CheckContext, confidence: float) -> bool:
    """A real UNKNOWN verdict - never dress missing data up as LOW risk."""
    if not (ctx.latest_version or ctx.latest_tag):
        return True
    if ctx.release_age_hours is None:
        return True
    if confidence < 0.45:
        return True
    return False


def _recommendation(
    *,
    score: int,
    level: str,
    insufficient: bool,
    age_days: Optional[float],
    threshold: int,
    min_age_days: float,
) -> tuple[str, Optional[int]]:
    if insufficient:
        return RECOMMEND_UNKNOWN, None
    if score >= 81:
        return RECOMMEND_AVOID, 7
    if score > threshold:
        return RECOMMEND_WAIT, 3
    if age_days is not None and age_days < min_age_days:
        remaining = max(1, int(math.ceil(min_age_days - age_days)))
        return RECOMMEND_WAIT, remaining
    return RECOMMEND_UPDATE, None
