"""Regression clustering: turn raw issue hits into *credible* regression signals.

Counting issues is easy and misleading: three reports from one author about three
different things are not one regression wave. This module grades every cluster on

* **severity**  - how bad the failure class is if real (CRITICAL/HIGH/MEDIUM/LOW),
* **independence** - how many distinct reporters actually saw it,
* **corroboration** - open state, maintainer confirmation, linked PRs,
  reproduction steps, explicit version mentions, matching failure signatures.

Only then does a cluster contribute to the Regression Signal, and the report can
always explain *why* (reports / reporters / open / confirmed / confidence).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .github_api import Issue

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"

SEVERITY_WEIGHTS = {
    SEVERITY_CRITICAL: 1.0,
    SEVERITY_HIGH: 0.75,
    SEVERITY_MEDIUM: 0.45,
    SEVERITY_LOW: 0.15,
}
CONFIDENCE_WEIGHTS = {CONFIDENCE_HIGH: 1.0, CONFIDENCE_MEDIUM: 0.6, CONFIDENCE_LOW: 0.3}

SEVERITY_RANK = {SEVERITY_CRITICAL: 3, SEVERITY_HIGH: 2, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 0}

#: Severity floor assigned to a cluster when issue data cannot be observed at all.
UNKNOWN_REGRESSION_FLOOR = 25


@dataclass(frozen=True)
class ClusterSpec:
    key: str
    zh: str
    en: str
    patterns: tuple[str, ...]
    base_severity: str
    critical_patterns: tuple[str, ...] = ()
    #: cluster is only considered when it has at least this many reports
    min_reports: int = 1


#: The nine regression clusters. Patterns deliberately require *failure
#: language*: "gateway: add metrics" is not a gateway regression.
CLUSTER_SPECS: tuple[ClusterSpec, ...] = (
    ClusterSpec(
        key="DATABASE",
        zh="数据库 / state.db",
        en="Database / state.db",
        base_severity=SEVERITY_HIGH,
        patterns=(
            r"state\.db[^.]{0,40}(corrupt|lock|error|fail|unreadable)",
            r"\b(?:database|db|sqlite)[^.]{0,30}(corrupt|locked|lock\b|error|fail|unreadable)",
            r"(corrupt|integrity)[^.]{0,30}(database|db|state\.db|sqlite)",
            r"\bdatabase migration\b[^.]{0,30}(fail|break|error)",
        ),
        critical_patterns=(r"\bcorrupt", r"data loss", r"unreadable", r"\bbrick"),
    ),
    ClusterSpec(
        key="SESSION",
        zh="Session / 会话数据",
        en="Session data",
        base_severity=SEVERITY_HIGH,
        patterns=(
            r"session[^.]{0,30}(lost|lost|missing|gone|corrupt|disappear|dropped)",
            r"(lost|lost|missing|corrupt)[^.]{0,20}sessions?",
            r"transcript[^.]{0,20}(lost|missing|corrupt)",
            r"resume[^.]{0,20}(wrong|failed|lost)",
        ),
        critical_patterns=(r"\blost\b", r"\bmissing\b", r"corrupt", r"data loss"),
    ),
    ClusterSpec(
        key="GATEWAY",
        zh="Gateway",
        en="Gateway",
        base_severity=SEVERITY_HIGH,
        patterns=(
            r"gateway[^.]{0,60}\b(?:fail|crash|broken|down|not running|won'?t|can'?t|refuse|deadlock|live-?lock|hang|loop|dies?|stuck|restart)",
            r"\b(?:fail|crash|broken|down|won'?t start|can'?t start)[^.]{0,40}gateway",
        ),
        critical_patterns=(
            r"crash[- ]?loop",
            r"repeatedly (crash|restart|die)",
            r"keeps? (crashing|restarting|dying)",
            r"won'?t start",
            r"live-?lock",
        ),
    ),
    ClusterSpec(
        key="CONFIG_MIGRATION",
        zh="配置迁移",
        en="Config migration",
        base_severity=SEVERITY_HIGH,
        patterns=(
            r"config[^.]{0,30}(migrat|broken|invalid|destroy|overwrit|corrupt)",
            r"migrat\w*[^.]{0,30}config",
            r"config\.ya?ml[^.]{0,20}(broken|invalid|corrupt|missing)",
        ),
        critical_patterns=(r"destroy", r"overwrit", r"wiped", r"data loss"),
    ),
    ClusterSpec(
        key="UPDATE_FAILURE",
        zh="升级 / 回滚",
        en="Update / rollback failure",
        base_severity=SEVERITY_HIGH,
        patterns=(
            r"(update|upgrade|install)[^.]{0,25}(fail|break|error|stuck|hang|refuse)",
            r"fail\w*[^.]{0,20}to (?:update|upgrade|install|reinstall)",
            r"can'?t (?:update|upgrade|install)",
            r"\brollback\b[^.]{0,25}(fail|broken|error)",
            r"hermes update[^.]{0,30}(fail|break|error|hang)",
        ),
        critical_patterns=(r"rollback fail", r"can'?t rollback", r"\bbrick", r"unusable after"),
    ),
    ClusterSpec(
        key="AUTH",
        zh="认证 / 凭证",
        en="Auth / credentials",
        base_severity=SEVERITY_HIGH,
        patterns=(
            r"auth\w*[^.]{0,30}(fail|break|invalid|error|reject)",
            r"credential[^.]{0,25}(lost|leak|invalid|missing|rotat)",
            r"token[^.]{0,25}(invalid|lost|rejected|expired|\b401\b)",
            r"login[^.]{0,20}(fail|loop|broken)",
            r"\b401\b[^.]{0,20}(error|unauthor)",
        ),
        critical_patterns=(r"credential loss", r"leak", r"all .{0,15}(keys|tokens)"),
    ),
    ClusterSpec(
        key="CRASH",
        zh="崩溃 / 进程异常",
        en="Crash / process failure",
        base_severity=SEVERITY_HIGH,
        patterns=(
            r"\bcrash(es|ed|ing)?\b",
            r"\bsegfault\b",
            r"\btraceback\b",
            r"\bpanic\b",
            r"\bcore dump\b",
            r"SIGTRAP",
            r"exit code \d+",
        ),
        critical_patterns=(r"segfault", r"\bpanic\b", r"core dump", r"crash[- ]?dump"),
    ),
    ClusterSpec(
        key="MCP",
        zh="MCP",
        en="MCP",
        base_severity=SEVERITY_MEDIUM,
        patterns=(
            r"\bmcp\b[^.]{0,35}(fail|broken|unusable|error|timeout|refuse|hang)",
            r"mcp server[^.]{0,25}(fail|break|error)",
            r"tool[^.]{0,20}not (?:available|found|working)[^.]{0,10}\bmcp\b",
        ),
        critical_patterns=(r"\bunusable\b", r"all (?:mcp|servers)", r"completely"),
    ),
    ClusterSpec(
        key="PROVIDER",
        zh="Provider / 模型接入",
        en="Provider / model access",
        base_severity=SEVERITY_MEDIUM,
        patterns=(
            r"provider[^.]{0,30}(fail|broken|error|unusable|refuse)",
            r"\bmodel[^.]{0,25}(broken|not work|fails?|unavailable|reject)",
            r"(api key|base url)[^.]{0,25}(fail|invalid|reject|error)",
        ),
        critical_patterns=(r"all providers", r"completely", r"\bevery\b"),
    ),
)

CLUSTER_BY_KEY = {spec.key: spec for spec in CLUSTER_SPECS}

#: How many issues per cluster get enriched (one API call each) - keeps the
#: run inside the unauthenticated rate budget.
DEFAULT_ENRICHMENT_LIMIT = 3


@dataclass
class IssueEnrichment:
    """Extra facts fetched for a single issue (comments / association)."""

    number: int
    maintainer_reply: bool = False
    maintainer_confirmed: bool = False
    linked_pr: bool = False
    reproduction: bool = False
    comments: int = 0
    author_association: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "maintainer_reply": self.maintainer_reply,
            "maintainer_confirmed": self.maintainer_confirmed,
            "linked_pr": self.linked_pr,
            "reproduction": self.reproduction,
            "comments": self.comments,
            "author_association": self.author_association,
        }


@dataclass
class RegressionCluster:
    """One graded regression cluster."""

    key: str
    zh: str
    en: str
    severity: str
    confidence: str
    reports: int = 0
    unique_reporters: int = 0
    open_count: int = 0
    maintainer_confirmed: int = 0
    linked_pr: int = 0
    with_reproduction: int = 0
    mentions_version: int = 0
    signatures: int = 0
    samples: list[Issue] = field(default_factory=list)
    notes_zh: list[str] = field(default_factory=list)
    notes_en: list[str] = field(default_factory=list)

    @property
    def contribution(self) -> float:
        """0..1 contribution to the regression signal."""
        severity_w = SEVERITY_WEIGHTS.get(self.severity, SEVERITY_WEIGHTS[SEVERITY_MEDIUM])
        confidence_w = CONFIDENCE_WEIGHTS.get(self.confidence, CONFIDENCE_WEIGHTS[CONFIDENCE_LOW])
        reporters = max(1, self.unique_reporters)
        report_w = min(1.0, 0.35 + 0.25 * (reporters - 1))
        return round(severity_w * confidence_w * report_w, 4)

    @property
    def is_severe(self) -> bool:
        return self.severity in {SEVERITY_CRITICAL, SEVERITY_HIGH}

    @property
    def is_active(self) -> bool:
        """Open reports, corroborated at least moderately."""
        return self.open_count > 0 and self.confidence in {CONFIDENCE_HIGH, CONFIDENCE_MEDIUM}

    def headline_zh(self) -> str:
        return f"{self.zh}: {self.severity} / 可信度 {self.confidence}"

    def headline_en(self) -> str:
        return f"{self.en}: {self.severity} / confidence {self.confidence}"

    def detail_zh(self) -> str:
        return (
            f"{self.reports} 个报告，{self.unique_reporters} 个独立报告人，"
            f"{self.open_count} 个 open，maintainer 确认 {self.maintainer_confirmed}，"
            f"linked PR {self.linked_pr}，含复现步骤 {self.with_reproduction}"
        )

    def detail_en(self) -> str:
        return (
            f"{self.reports} reports, {self.unique_reporters} unique reporters, "
            f"{self.open_count} open, {self.maintainer_confirmed} maintainer-confirmed, "
            f"{self.linked_pr} linked PR(s), {self.with_reproduction} with reproduction"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label_zh": self.zh,
            "label_en": self.en,
            "severity": self.severity,
            "confidence": self.confidence,
            "reports": self.reports,
            "unique_reporters": self.unique_reporters,
            "open": self.open_count,
            "maintainer_confirmed": self.maintainer_confirmed,
            "linked_pr": self.linked_pr,
            "with_reproduction": self.with_reproduction,
            "mentions_version": self.mentions_version,
            "signatures": self.signatures,
            "contribution": self.contribution,
            "samples": [issue.to_dict() for issue in self.samples[:3]],
            "notes_zh": self.notes_zh,
            "notes_en": self.notes_en,
        }


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #

_COMPILED: dict[str, list[re.Pattern[str]]] = {
    spec.key: [re.compile(p, re.IGNORECASE) for p in spec.patterns] for spec in CLUSTER_SPECS
}
_CRITICAL_COMPILED: dict[str, list[re.Pattern[str]]] = {
    spec.key: [re.compile(p, re.IGNORECASE) for p in spec.critical_patterns] for spec in CLUSTER_SPECS
}


def cluster_keys_for(issue: Issue, *, include_body: bool = True) -> list[str]:
    """Which clusters an issue belongs to (title + labels first, then body)."""
    primary = f"{issue.title} {issue.label_text}"
    keys = [key for key, patterns in _COMPILED.items() if any(p.search(primary) for p in patterns)]
    if keys or not include_body:
        return keys
    body = (issue.body or "")[:1500]
    if not body:
        return []
    return [key for key, patterns in _COMPILED.items() if any(p.search(body) for p in patterns)]


def severity_for(key: str, text: str) -> str:
    """Cluster severity, promoted to CRITICAL when the text says so."""
    spec = CLUSTER_BY_KEY[key]
    for pattern in _CRITICAL_COMPILED.get(key, []):
        if pattern.search(text):
            return SEVERITY_CRITICAL
    return spec.base_severity


_REPRODUCTION_MARKERS = (
    "steps to reproduce",
    "to reproduce",
    "repro steps",
    "how to reproduce",
    "traceback",
    "error:",
    "stack trace",
    "```",
)
_VERSION_MENTION_RE = re.compile(r"\bv?\d+\.\d+\.\d+\b|\bv\d{4}\.\d{1,2}\.\d{1,2}\b")
_PR_REFERENCE_RE = re.compile(r"(?:fixed by|fixes|closes|by|see)\s+#(\d{3,6})|(?:^|\s)#(\d{3,6})\b")
_MAINTAINER_LABELS = ("confirmed", "maintainer", "triage", "accepted")


def _title_signature(title: str) -> str:
    """Crude failure signature: the meaningful words of the title."""
    words = re.findall(r"[a-z0-9_]+", title.lower())
    stop = {
        "bug", "error", "issue", "the", "a", "an", "in", "on", "of", "to", "and", "with", "for",
        "after", "when", "is", "not", "no", "fails", "failed", "failure", "problem", "broken",
    }
    return " ".join(w for w in words if w not in stop)[:120]


def _text_of(issue: Issue) -> str:
    return f"{issue.title}\n{issue.body or ''}"


def build_clusters(
    issues: Iterable[Issue],
    *,
    enrichment: Optional[dict[int, IssueEnrichment]] = None,
    release_version: Optional[str] = None,
    release_tag: Optional[str] = None,
) -> list[RegressionCluster]:
    """Grade every cluster found in the scanned issues."""
    enrichment = enrichment or {}
    grouped: dict[str, list[Issue]] = {spec.key: [] for spec in CLUSTER_SPECS}
    for issue in issues:
        for key in cluster_keys_for(issue):
            grouped[key].append(issue)

    clusters: list[RegressionCluster] = []
    for spec in CLUSTER_SPECS:
        members = grouped.get(spec.key) or []
        if len(members) < spec.min_reports:
            continue
        clusters.append(
            _grade_cluster(
                spec,
                members,
                enrichment=enrichment,
                release_version=release_version,
                release_tag=release_tag,
            )
        )
    clusters.sort(key=lambda c: (SEVERITY_RANK.get(c.severity, 0), c.contribution), reverse=True)
    return clusters


def _grade_cluster(
    spec: ClusterSpec,
    members: list[Issue],
    *,
    enrichment: dict[int, IssueEnrichment],
    release_version: Optional[str],
    release_tag: Optional[str],
) -> RegressionCluster:
    reports = len(members)
    authors = {(issue.author or "").strip().lower() for issue in members if issue.author}
    unique_reporters = len(authors) if authors else reports
    open_count = sum(1 for issue in members if issue.state.lower() == "open")

    maintainer_confirmed = 0
    maintainer_replies = 0
    linked_pr = 0
    with_reproduction = 0
    mentions_version = 0
    signatures: set[str] = set()
    notes_zh: list[str] = []
    notes_en: list[str] = []

    for issue in members:
        text = _text_of(issue)
        extra = enrichment.get(issue.number)
        if extra is not None:
            maintainer_replies += 1 if extra.maintainer_reply else 0
            maintainer_confirmed += 1 if extra.maintainer_confirmed else 0
            linked_pr += 1 if extra.linked_pr else 0
            with_reproduction += 1 if extra.reproduction else 0
            if extra.author_association:
                issue.author_association = extra.author_association
        if any(marker in text.lower() for marker in _REPRODUCTION_MARKERS):
            with_reproduction += 1
        if _PR_REFERENCE_RE.search(text):
            linked_pr += 1
        if release_version and release_version in text:
            mentions_version += 1
        elif release_tag and release_tag.lstrip("v") in text:
            mentions_version += 1
        if issue.state.lower() == "open" and any(
            label.lower() in _MAINTAINER_LABELS for label in issue.labels
        ):
            maintainer_confirmed += 1
        signatures.add(_title_signature(issue.title))

    severity = max(
        (severity_for(spec.key, _text_of(issue)) for issue in members),
        key=lambda sev: SEVERITY_RANK.get(sev, 0),
    )
    confidence = grade_confidence(
        reports=reports,
        unique_reporters=unique_reporters,
        open_count=open_count,
        maintainer_confirmed=maintainer_confirmed,
        linked_pr=linked_pr,
        with_reproduction=with_reproduction,
        mentions_version=mentions_version,
        signature_groups=len(signatures),
    )
    if unique_reporters == 1 and reports > 1:
        notes_zh.append("所有报告来自同一位报告人：按 1 个独立报告人计算")
        notes_en.append("all reports come from one author: counted as a single reporter")
    if len(signatures) == reports and reports > 1:
        notes_zh.append("报告描述的故障各不相同（可能是不同问题）")
        notes_en.append("reports describe different failures (may be separate problems)")
    if maintainer_replies:
        notes_zh.append(f"{maintainer_replies} 个 Issue 有 maintainer 参与回复")
        notes_en.append(f"{maintainer_replies} issue(s) have maintainer replies")

    return RegressionCluster(
        key=spec.key,
        zh=spec.zh,
        en=spec.en,
        severity=severity,
        confidence=confidence,
        reports=reports,
        unique_reporters=unique_reporters,
        open_count=open_count,
        maintainer_confirmed=min(maintainer_confirmed, reports),
        linked_pr=min(linked_pr, reports),
        with_reproduction=min(with_reproduction, reports),
        mentions_version=mentions_version,
        signatures=len(signatures),
        samples=sorted(members, key=lambda i: (i.state.lower() != "open", -i.comments))[:3],
        notes_zh=notes_zh,
        notes_en=notes_en,
    )


def grade_confidence(
    *,
    reports: int,
    unique_reporters: int,
    open_count: int,
    maintainer_confirmed: int,
    linked_pr: int,
    with_reproduction: int,
    mentions_version: int,
    signature_groups: int,
) -> str:
    """Credibility of a cluster, 0..1, mapped to LOW/MEDIUM/HIGH.

    Weights (documented, no magic model):

    * 0.30 distinct reporters (1 reporter counts 0.15 - one voice is not a wave)
    * 0.15 issue still open
    * 0.15 maintainer confirmation
    * 0.10 linked PR
    * 0.10 reproduction details
    * 0.10 explicit version mention
    * 0.10 several reports sharing one failure signature
    """
    score = 0.0
    score += 0.30 * min(1.0, max(0.15, 0.15 * unique_reporters)) if unique_reporters == 1 else 0.30
    score += 0.15 * min(1.0, open_count / 2.0)
    score += 0.15 * (1.0 if maintainer_confirmed else 0.0)
    score += 0.10 * (1.0 if linked_pr else 0.0)
    score += 0.10 * (1.0 if with_reproduction else 0.0)
    score += 0.10 * (1.0 if mentions_version else 0.0)
    score += 0.10 * (1.0 if (reports > 1 and signature_groups < reports) else 0.0)

    if score >= 0.65:
        return CONFIDENCE_HIGH
    if score >= 0.35:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


# --------------------------------------------------------------------------- #
# regression signal
# --------------------------------------------------------------------------- #


@dataclass
class RegressionSignal:
    """The Regression Signal (0-100) plus the floor used when data is missing."""

    value: Optional[int] = None
    floor: int = 0
    cluster_ratio: float = 0.0
    volume_ratio: float = 0.0
    unknown: bool = False
    reasons_zh: list[str] = field(default_factory=list)
    reasons_en: list[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        if self.value is None:
            return f"UNKNOWN (assume >= {self.floor})"
        if self.unknown:
            return f">= {self.value}"
        return str(self.value)

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "floor": self.floor,
            "unknown": self.unknown,
            "cluster_ratio": round(self.cluster_ratio, 4),
            "volume_ratio": round(self.volume_ratio, 4),
            "reasons_zh": self.reasons_zh,
            "reasons_en": self.reasons_en,
        }


#: Clusters combine with a probabilistic OR, but each one only counts at this
#: fraction of its raw weight: with a noisy repository there can be a dozen
#: clusters and the signal would otherwise saturate at 100.
CLUSTER_WEIGHT = 0.6

#: The whole cluster ratio is graded by how trustworthy the issue data is
#: (Issue Signal Confidence): 0.55 at zero confidence, 1.0 at full confidence.
EVIDENCE_FLOOR = 0.55


def regression_signal(
    clusters: Iterable[RegressionCluster],
    *,
    volume_ratio: float = 0.0,
    signal_confidence: Optional[int] = None,
    unavailable: bool = False,
    unavailable_reason: str = "",
) -> RegressionSignal:
    """Combine cluster evidence into the Regression Signal.

    Clusters combine with a probabilistic OR (independent failure classes), each
    damped to ``CLUSTER_WEIGHT`` and the total graded by the issue-signal
    confidence, then the raw issue-volume component (from ``risk.issue_points``)
    is folded in at half weight because counts alone are the weakest evidence.
    """
    if unavailable:
        return RegressionSignal(
            value=None,
            floor=UNKNOWN_REGRESSION_FLOOR,
            unknown=True,
            reasons_zh=[f"Issue 数据不可用，无法评估回归信号（按 >= {UNKNOWN_REGRESSION_FLOOR} 处理）: {unavailable_reason}"],
            reasons_en=[
                f"issue data unavailable, regression signal cannot be measured "
                f"(assumed >= {UNKNOWN_REGRESSION_FLOOR}): {unavailable_reason}"
            ],
        )

    cluster_list = list(clusters)
    product = 1.0
    reasons_zh: list[str] = []
    reasons_en: list[str] = []
    for cluster in cluster_list:
        damped = max(0.0, min(1.0, cluster.contribution * CLUSTER_WEIGHT))
        product *= 1.0 - damped
        reasons_zh.append(
            f"{cluster.zh}：{cluster.severity} × 可信度 {cluster.confidence}"
            f"（{cluster.reports} 报告 / {cluster.unique_reporters} 报告人）"
        )
        reasons_en.append(
            f"{cluster.en}: {cluster.severity} x confidence {cluster.confidence} "
            f"({cluster.reports} reports / {cluster.unique_reporters} reporters)"
        )
    cluster_ratio = 1.0 - product
    if signal_confidence is not None:
        evidence = EVIDENCE_FLOOR + (1.0 - EVIDENCE_FLOOR) * max(0.0, min(100, signal_confidence)) / 100.0
        if evidence < 1.0:
            reasons_zh.append(f"按 Issue 数据可信度折算（{signal_confidence}/100 → ×{evidence:.2f}）")
            reasons_en.append(
                f"graded by issue-signal confidence ({signal_confidence}/100 -> x{evidence:.2f})"
            )
        cluster_ratio *= evidence
    ratio = 1.0 - (1.0 - cluster_ratio) * (1.0 - 0.5 * max(0.0, min(1.0, volume_ratio)))
    value = int(round(100 * max(0.0, min(1.0, ratio))))
    if volume_ratio > 0:
        reasons_zh.append(f"Issue 命中量组件按半权重计入（{volume_ratio:.2f}）")
        reasons_en.append(f"raw issue-volume component folded in at half weight ({volume_ratio:.2f})")
    return RegressionSignal(
        value=value,
        floor=value,
        cluster_ratio=cluster_ratio,
        volume_ratio=volume_ratio,
        reasons_zh=reasons_zh,
        reasons_en=reasons_en,
    )
