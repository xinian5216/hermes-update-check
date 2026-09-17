"""Personal impact, core-feature readiness and systemic critical risk.

Phase 3's core claim: *global instability does not necessarily mean personal
unusability*. Two numbers carry that claim, and both are explainable end to end:

``Personal Impact`` (0-100)
    How much the known regressions of the candidate release would hit the workflows
    this user actually runs. A regression in a feature the profile marks ``unused``
    contributes exactly zero, which is the whole point::

        contribution(cluster) = severity_weight x confidence_weight      # 0..1
        personal(feature)     = min(1, sum of contributions) x level_weight(feature)
        PersonalImpact        = 100 x (1 - prod(1 - personal(feature)))

``Core Feature Readiness`` (0-100)
    Same evidence, damped and weighted for *availability* rather than *exposure*:
    small problems barely move it, a well-corroborated severe regression on a
    critical feature destroys it. This is the number that drives the verdict::

        q(feature)     = min(1, sum of contributions) ** READINESS_DAMPING x level_weight
        Readiness      = 100 x (1 - prod(1 - q(feature)))

``Systemic Critical Risk``
    Categories that are *not* negotiable and not profile-dependent: data
    corruption, session loss, credential loss, config destruction, installation
    corruption, rollback failure, startup failure. A high-confidence report in one of
    these blocks the update no matter how little the user touches the feature.

Nothing here reads the network or a config value; it is a pure function of the
clusters, the profile and (for rollback) a local safety probe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .clusters import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_WEIGHTS,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    SEVERITY_RANK,
    SEVERITY_WEIGHTS,
    RegressionCluster,
)
from .risk import level_for_score
from .usage_profile import (
    LEVEL_CRITICAL,
    LEVEL_IMPORTANT,
    LEVEL_UNUSED,
    LEVEL_WEIGHTS,
    UsageProfile,
)

#: Readiness is deliberately less sensitive than impact: a single medium report
#: should not make a working installation look broken (see the module docstring).
READINESS_DAMPING = 1.6

#: Verdict boundaries on Core Feature Readiness.
READINESS_SAFE_MIN = 85
READINESS_ACCEPTABLE_MIN = 60

#: Confidence required before a *critical* feature issue is treated as real.
CRITICAL_BROKEN_CONFIDENCE = CONFIDENCE_HIGH

FEATURE_OK = "OK"
FEATURE_WARN = "WARN"
FEATURE_FAIL = "FAIL"
FEATURE_UNUSED = "UNUSED"


@dataclass(frozen=True)
class SystemicSpec:
    key: str
    zh: str
    en: str
    clusters: tuple[str, ...]
    patterns: tuple[str, ...]


#: System-wide risks: independent of the usage profile (doc section 13).
#:
#: The patterns deliberately require the *thing* and its *failure* to sit next to
#: each other: "session restore - missing AttributeError handling" is a bug, but it
#: is not "session loss", and a block has to be defensible.
SYSTEMIC_SPECS: tuple[SystemicSpec, ...] = (
    SystemicSpec(
        key="DATA_CORRUPTION",
        zh="数据库损坏",
        en="Database corruption",
        clusters=("DATABASE",),
        patterns=(
            r"(?:state\.db|database|\bdb\b|sqlite)[^.]{0,30}(?:corrupt|unreadable|integrity)",
            r"(?:corrupt|unreadable)[^.]{0,25}(?:state\.db|database|\bdb\b|sqlite)",
            r"\bdata loss\b",
        ),
    ),
    SystemicSpec(
        key="SESSION_LOSS",
        zh="Session 丢失",
        en="Session loss",
        clusters=("SESSION",),
        patterns=(
            # "lost to <something>" is idiomatic for "wasted"; only data loss counts
            r"sessions?[^.]{0,30}(?:lost|wiped|deleted|destroyed|disappear|corrupt|unrecoverable)(?!\s+to\b)",
            r"(?:lost|wiped|deleted|destroyed)\s+(?:all\s+|the\s+|my\s+|their\s+)?sessions?\b(?!\s+to\b)",
            r"sessions?\s+(?:data|history|transcripts?|contents?)[^.]{0,25}(?:lost|missing|gone|wiped|corrupt)",
            r"transcripts?[^.]{0,25}(?:lost|missing|corrupt|wiped)",
            r"\bdata loss\b",
        ),
    ),
    SystemicSpec(
        key="CREDENTIAL_LOSS",
        zh="凭证丢失 / 泄露",
        en="Credential loss",
        clusters=("AUTH",),
        patterns=(
            r"credentials?[^.]{0,25}(?:lost|wiped|deleted|destroyed|leaked?)",
            r"(?:lost|wiped|deleted|destroyed)[^.]{0,25}credentials?",
            r"\bcredential loss\b",
            r"leaked?[^.]{0,25}(?:credential|token|api key|secret)s?",
            r"auth\.json[^.]{0,25}(?:lost|wiped|deleted|corrupt|empty)",
        ),
    ),
    SystemicSpec(
        key="CONFIG_DESTRUCTION",
        zh="配置损坏",
        en="Config destruction",
        clusters=("CONFIG_MIGRATION",),
        patterns=(
            r"config[^.]{0,25}(?:destroyed|wiped|overwritten|corrupt|emptied)",
            r"(?:destroyed|wiped|overwritten|emptied)[^.]{0,25}config",
            r"migration[^.]{0,25}(?:destroyed|wiped|overwritten|corrupt)",
        ),
    ),
    SystemicSpec(
        key="INSTALL_CORRUPTION",
        zh="安装损坏",
        en="Installation corruption",
        clusters=("UPDATE_FAILURE",),
        patterns=(
            r"(?:install|installation|venv|environment)[^.]{0,25}(?:corrupt|broken|unusable)",
            r"\bbrick(?:ed|s)?\b",
            r"unusable after[^.]{0,25}(?:update|upgrade)",
            r"(?:update|upgrade)[^.]{0,25}(?:corrupt|broke|destroy)",
        ),
    ),
    SystemicSpec(
        key="ROLLBACK_FAILURE",
        zh="回滚失败",
        en="Rollback failure",
        clusters=("UPDATE_FAILURE",),
        patterns=(
            r"rollback[^.]{0,25}(?:fail|broken|error|not work|unavailable)",
            r"can'?t rollback",
            r"rollback[^.]{0,15}(?:does not|doesn'?t) work",
        ),
    ),
    SystemicSpec(
        key="STARTUP_FAILURE",
        zh="Hermes 无法启动",
        en="Hermes cannot start",
        clusters=("CRASH",),
        patterns=(
            # clause-shaped on purpose: "startup crash is hard to tell apart" is talk
            r"(?:hermes|the app|the cli)[^.]{0,20}(?:won'?t|fails? to|refus(?:es|e) to|cannot|can'?t|unable to)[^.]{0,10}(?:start|launch)",
            r"(?:crashes?|dies?|hangs?|fails?)[^.]{0,12}(?:on|at|during|right after|immediately after)\s+startup",
            r"startup[^.]{0,20}(?:crash|failure)[^.]{0,25}(?:prevents|blocks|leaves|unusable|crash loop|loop)",
            r"won'?t start\b",
            r"fails? to (?:launch|start up)\b",
        ),
    ),
    SystemicSpec(
        key="ALL_PROVIDERS_DOWN",
        zh="所有 Provider 不可用",
        en="All providers unavailable",
        clusters=("PROVIDER", "AUTH"),
        patterns=(
            r"all providers",
            r"every provider",
            r"provider system[^.]{0,25}(?:broken|down|dead|unavailable)",
        ),
    ),
)

_SYSTEMIC_COMPILED = {
    spec.key: [re.compile(p, re.IGNORECASE) for p in spec.patterns] for spec in SYSTEMIC_SPECS
}

#: Words that, shortly before a match, mean the text is *talking about* preventing a
#: failure rather than reporting one ("halts to prevent SQLite corruption").
_NEGATION_WINDOW = 70
_NEGATION_MARKERS = (
    "prevent",
    "avoid",
    "protect",
    "mitigat",
    "risk of",
    "instead of",
    "rather than",
    "to catch",
    "so that",
    "would have",
)


def _first_unnegated_match(patterns: Sequence[re.Pattern[str]], text: str) -> Optional[tuple[str, str]]:
    """First pattern hit that is not inside a "we prevent X" clause.

    Returns ``(pattern, matched_text)`` or ``None``.
    """
    for pattern in patterns:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - _NEGATION_WINDOW) : match.start()].lower()
            if any(marker in window for marker in _NEGATION_MARKERS):
                continue
            return pattern.pattern, match.group(0).strip()
    return None


@dataclass
class SystemicRisk:
    """One system-wide risk category and whether the evidence matches it."""

    key: str
    zh: str
    en: str
    detected: bool = False
    confidence: str = ""
    cluster_key: str = ""
    evidence_zh: str = ""
    evidence_en: str = ""
    source: str = "clusters"  # clusters | local
    #: independent corroboration (>= 2 reporters, a maintainer confirmation, or two
    #: independent reproductions) - a single unconfirmed report may not block
    corroborated: bool = False
    reports: int = 0
    unique_reporters: int = 0
    maintainer_confirmed: int = 0

    @property
    def blocking(self) -> bool:
        """High confidence *and* corroboration, or a local measurement (doc section 7)."""
        if not self.detected:
            return False
        if self.source == "local":
            return True
        return self.confidence == CONFIDENCE_HIGH and self.corroborated

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label_zh": self.zh,
            "label_en": self.en,
            "detected": self.detected,
            "blocking": self.blocking,
            "corroborated": self.corroborated,
            "confidence": self.confidence,
            "cluster": self.cluster_key,
            "source": self.source,
            "reports": self.reports,
            "unique_reporters": self.unique_reporters,
            "maintainer_confirmed": self.maintainer_confirmed,
            "evidence_zh": self.evidence_zh,
            "evidence_en": self.evidence_en,
        }


#: Predicates that claim a thing is *not available* (as opposed to "has a bug").
#: Readiness measures availability, so only these can sink it - a known bug in an
#: edge case of a feature you use still shows up in Personal Impact.
AVAILABILITY_PREDICATES: tuple[str, ...] = (
    r"unusable",
    r"completely broken",
    r"(?:does|do)(?:n'?t| not) work",
    r"no longer works?",
    r"stopped working",
    r"stops? working",
    r"cannot (?:be used|start|run|launch|connect|log ?in|access)",
    r"can'?t (?:be used|start|run|launch|connect|log ?in|access)",
    r"fails? (?:every|all|always|repeatedly|consistently|constantly)",
    r"crash(?:es)? loop",
    r"kills? the (?:gateway|agent|process|app|session)",
    r"unavailable",
    r"unrecoverable",
    r"\bis broken\b",
    r"\bare broken\b",
    r"broken since",
)
_AVAILABILITY_COMPILED = tuple(re.compile(p, re.IGNORECASE) for p in AVAILABILITY_PREDICATES)

#: Vocabulary per feature, so "gateway" and "cannot start" have to appear in the
#: same sentence - a passing mention deep inside a discussion must not count.
_FEATURE_VOCAB: dict[str, str] = {
    "cli_agent": r"hermes|cli|agent|the app",
    "desktop": r"desktop|the app|gui",
    "sessions": r"sessions?|transcripts?|conversations?",
    "tools": r"tools?|tool calls?|kanban|cron|cronjob|workers?|skills?|plugins?",
    "mcp": r"mcp|mcp servers?",
    "gateway": r"gateways?",
    "telegram": r"telegram|weixin|discord|slack|bot\b",
    "web_tools": r"web search|search tool|web fetch",
    "browser_tools": r"browser|playwright|cdp|chromium",
    "docker": r"docker|containers?",
    "config": r"config|configuration|settings",
}
_PROVIDER_VOCAB = r"providers?|api key|endpoint"


def _negated(text: str, start: int) -> bool:
    window = text[max(0, start - 70) : start].lower()
    return any(marker in window for marker in _NEGATION_MARKERS)


def feature_claims_unavailable(feature_key: str, text: str) -> bool:
    """Does this evidence claim *this feature* is unavailable?

    Requires the feature's own vocabulary and an unavailability predicate in the
    same sentence, within 40 characters, with no "we prevent/avoid X" nearby.
    """
    if not text:
        return False
    if feature_key.startswith("providers."):
        name = feature_key.split(".", 1)[1]
        subject = rf"(?:{_PROVIDER_VOCAB}|{re.escape(name)})"
    elif feature_key == "providers":
        subject = _PROVIDER_VOCAB
    else:
        subject = _FEATURE_VOCAB.get(feature_key, re.escape(feature_key))

    forward = re.compile(rf"(?:{subject})[^.]{{0,40}}(?:{'|'.join(AVAILABILITY_PREDICATES)})", re.IGNORECASE)
    for match in forward.finditer(text):
        if not _negated(text, match.start()):
            return True
    return False


def claims_unavailability(text: str) -> bool:
    """Does this evidence contain any *unnegated* unavailability claim at all?"""
    if not text:
        return False
    for pattern in _AVAILABILITY_COMPILED:
        for match in pattern.finditer(text):
            if not _negated(text, match.start()):
                return True
    return False


@dataclass
class FeatureImpact:
    """One feature of the profile and what the candidate release does to it."""

    key: str
    level: str
    weight: float
    status: str = FEATURE_OK
    severity: str = ""
    confidence: str = ""
    cluster_keys: list[str] = field(default_factory=list)
    personal: float = 0.0
    readiness_loss: float = 0.0
    unavailable: bool = False

    @property
    def is_unused(self) -> bool:
        return self.weight <= 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "level": self.level,
            "weight": self.weight,
            "status": self.status,
            "severity": self.severity,
            "confidence": self.confidence,
            "clusters": self.cluster_keys,
            "personal": round(self.personal, 4),
            "readiness_loss": round(self.readiness_loss, 4),
            "unavailable": self.unavailable,
        }


@dataclass
class PersonalReadiness:
    """The phase-3 bundle: impact + readiness + systemic risk."""

    profile: UsageProfile
    impact: Optional[int] = None
    impact_level: str = "UNKNOWN"
    readiness: Optional[int] = None
    readiness_level: str = "UNKNOWN"
    features: list[FeatureImpact] = field(default_factory=list)
    systemic: list[SystemicRisk] = field(default_factory=list)
    reasons_zh: list[str] = field(default_factory=list)
    reasons_en: list[str] = field(default_factory=list)
    unavailable: bool = False

    @property
    def active_systemic(self) -> list[SystemicRisk]:
        return [risk for risk in self.systemic if risk.detected]

    @property
    def blocking_systemic(self) -> list[SystemicRisk]:
        return [risk for risk in self.systemic if risk.blocking]

    @property
    def critical_broken(self) -> list[FeatureImpact]:
        """Critical features with a *confirmed* (high-confidence) severe regression."""
        return [
            feature
            for feature in self.features
            if feature.level == LEVEL_CRITICAL and feature.status == FEATURE_FAIL
        ]

    @property
    def critical_suspect(self) -> list[FeatureImpact]:
        """Critical features with a severe but not yet confirmed regression."""
        return [
            feature
            for feature in self.features
            if feature.level == LEVEL_CRITICAL and feature.status == FEATURE_WARN and feature.severity in {SEVERITY_CRITICAL, SEVERITY_HIGH}
        ]

    @property
    def used_features(self) -> list[FeatureImpact]:
        return [feature for feature in self.features if not feature.is_unused]

    @property
    def unused_with_issues(self) -> list[FeatureImpact]:
        return [feature for feature in self.features if feature.is_unused and feature.cluster_keys]

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": not self.unavailable,
            "profile_source": self.profile.source,
            "profile": self.profile.to_dict(),
            "personal_impact": self.impact,
            "personal_impact_level": self.impact_level,
            "core_readiness": self.readiness,
            "core_readiness_level": self.readiness_level,
            "features": [feature.to_dict() for feature in self.features],
            "systemic": [risk.to_dict() for risk in self.systemic],
            "reasons_zh": self.reasons_zh,
            "reasons_en": self.reasons_en,
        }


def cluster_text(cluster: RegressionCluster) -> str:
    """Evidence text for a cluster: every sample title plus a slice of each body."""
    parts: list[str] = []
    blob = getattr(cluster, "evidence_text", "")
    if blob:
        parts.append(str(blob))
    for issue in getattr(cluster, "samples", []) or []:
        title = getattr(issue, "title", "") or ""
        parts.append(str(title))
        body = getattr(issue, "body", "") or ""
        if body:
            parts.append(str(body)[:800])
    return "\n".join(part for part in parts if part)


def _cluster_confidence_weight(cluster: RegressionCluster) -> float:
    return CONFIDENCE_WEIGHTS.get(cluster.confidence, CONFIDENCE_WEIGHTS[CONFIDENCE_LOW])


def _cluster_severity_weight(cluster: RegressionCluster) -> float:
    return SEVERITY_WEIGHTS.get(cluster.severity, SEVERITY_WEIGHTS[SEVERITY_MEDIUM])


def detect_systemic_risks(
    clusters: Iterable[RegressionCluster],
    *,
    cluster_source: Optional[dict[str, list[RegressionCluster]]] = None,
) -> list[SystemicRisk]:
    """Which system-wide risk categories the evidence actually supports.

    ``cluster_source`` overrides the default key->clusters index (used by tests);
    by default the clusters are indexed by their own key.
    """
    cluster_list = list(clusters)
    index: dict[str, list[RegressionCluster]] = {}
    for cluster in cluster_list:
        index.setdefault(cluster.key, []).append(cluster)
    if cluster_source:
        for key, value in cluster_source.items():
            index.setdefault(key, list(value))

    risks: list[SystemicRisk] = []
    for spec in SYSTEMIC_SPECS:
        patterns = _SYSTEMIC_COMPILED[spec.key]
        best: Optional[RegressionCluster] = None
        for key in spec.clusters:
            for cluster in index.get(key, []):
                if cluster.open_count <= 0:
                    continue
                if SEVERITY_RANK.get(cluster.severity, 0) < SEVERITY_RANK[SEVERITY_HIGH]:
                    continue
                if best is None or SEVERITY_RANK.get(cluster.severity, 0) > SEVERITY_RANK.get(best.severity, 0):
                    best = cluster
        text = cluster_text(best) if best is not None else ""
        hit = _first_unnegated_match(patterns, text) if text else None
        detected = hit is not None and best is not None
        corroborated = bool(
            best is not None
            and (best.unique_reporters >= 2 or best.maintainer_confirmed >= 1 or best.with_reproduction >= 2)
        )
        risks.append(
            SystemicRisk(
                key=spec.key,
                zh=spec.zh,
                en=spec.en,
                detected=detected,
                confidence=(best.confidence if (detected and best is not None) else ""),
                cluster_key=(best.key if best is not None else ""),
                corroborated=corroborated if detected else False,
                reports=(best.reports if best is not None else 0),
                unique_reporters=(best.unique_reporters if best is not None else 0),
                maintainer_confirmed=(best.maintainer_confirmed if best is not None else 0),
                evidence_zh=(
                    f"{best.zh}：{best.severity} / 可信度 {best.confidence}"
                    f"（{best.reports} 个报告 / {best.unique_reporters} 位报告人）"
                    + ("" if corroborated else "，尚无独立佐证")
                    if detected and best is not None
                    else ""
                ),
                evidence_en=(
                    f"{best.en}: {best.severity} / confidence {best.confidence} "
                    f"({best.reports} report(s) / {best.unique_reporters} reporter(s))"
                    + ("" if corroborated else ", not independently corroborated yet")
                    if detected and best is not None
                    else ""
                ),
            )
        )
    return risks


def local_rollback_risk(status: str, *, detail_zh: str = "", detail_en: str = "") -> Optional[SystemicRisk]:
    """Turn a failing rollback-safety probe into a systemic risk entry."""
    from .rollback_safety import SAFETY_FAIL

    if status != SAFETY_FAIL:
        return None
    spec = next(spec for spec in SYSTEMIC_SPECS if spec.key == "ROLLBACK_FAILURE")
    return SystemicRisk(
        key=spec.key,
        zh=f"{spec.zh}（本地检查）",
        en=f"{spec.en} (local check)",
        detected=True,
        confidence=CONFIDENCE_HIGH,
        cluster_key="",
        evidence_zh=detail_zh or "本地 rollback 前置检查未通过",
        evidence_en=detail_en or "local pre-update rollback check failed",
        source="local",
    )


def compute_personal_readiness(
    profile: UsageProfile,
    clusters: Sequence[RegressionCluster],
    *,
    extra_systemic: Sequence[SystemicRisk] = (),
    impact_feature_keys: Optional[dict[str, list[str]]] = None,
) -> PersonalReadiness:
    """Impact + readiness from clusters and the profile.

    ``impact_feature_keys`` maps a cluster key onto feature keys when the cluster
    does not carry ``affected_features`` itself (older data, tests).
    """
    result = PersonalReadiness(profile=profile)
    cluster_list = [cluster for cluster in clusters if cluster.open_count > 0]
    if not cluster_list:
        result.impact = 0
        result.impact_level = level_for_score(0)
        result.readiness = 100
        result.readiness_level = level_for_score(100)
        result.features = _feature_rows(profile, {}, {})
        result.systemic = list(extra_systemic) + detect_systemic_risks(clusters)
        result.reasons_zh.append("没有 open 的回归报告命中任何已知类别：个人影响 0，核心可用性 100")
        result.reasons_en.append("no open regression reports in any known class: personal impact 0, readiness 100")
        return result

    per_feature_contrib: dict[str, float] = {}
    per_feature_meta: dict[str, dict[str, Any]] = {}

    for cluster in cluster_list:
        affected = list(getattr(cluster, "affected_features", []) or [])
        if not affected and impact_feature_keys:
            affected = list(impact_feature_keys.get(cluster.key, []))
        if not affected:
            affected = [cluster.key.lower()]
        contribution = _cluster_severity_weight(cluster) * _cluster_confidence_weight(cluster)
        cluster_evidence = cluster_text(cluster)
        for feature_key in affected:
            per_feature_contrib[feature_key] = min(1.0, per_feature_contrib.get(feature_key, 0.0) + contribution)
            meta = per_feature_meta.setdefault(
                feature_key, {"severity": "", "confidence": "", "clusters": [], "unavailable": False}
            )
            if SEVERITY_RANK.get(cluster.severity, 0) > SEVERITY_RANK.get(meta["severity"], -1):
                meta["severity"] = cluster.severity
                meta["confidence"] = cluster.confidence
            elif cluster.confidence == CONFIDENCE_HIGH and meta["confidence"] != CONFIDENCE_HIGH:
                meta["confidence"] = cluster.confidence
            if feature_claims_unavailable(feature_key, cluster_evidence):
                meta["unavailable"] = True
            if cluster.key not in meta["clusters"]:
                meta["clusters"].append(cluster.key)

    personal_values: dict[str, float] = {}
    readiness_values: dict[str, float] = {}
    for feature_key, total in per_feature_contrib.items():
        level = profile.level(feature_key)
        weight = profile.weight(feature_key)
        info = per_feature_meta.get(feature_key, {})
        personal_values[feature_key] = min(1.0, total) * weight
        # readiness only drops for *availability* claims; a critical feature that is
        # buggy (but not unavailable) still counts, at a discount (doc: "大幅下降")
        if info.get("unavailable"):
            readiness_factor = 1.0
        elif level == LEVEL_CRITICAL:
            readiness_factor = 0.6
        else:
            readiness_factor = 0.0
        readiness_values[feature_key] = (min(1.0, total) ** READINESS_DAMPING) * weight * readiness_factor

    impact_ratio = 1.0 - _product(personal_values.values())
    readiness_ratio = 1.0 - _product(readiness_values.values())

    broken = [self_feature for self_feature in per_feature_meta]
    result.features = _feature_rows(profile, per_feature_contrib, per_feature_meta)
    result.impact = round(100 * max(0.0, min(1.0, impact_ratio)))
    result.readiness = round(100 * (1.0 - max(0.0, min(1.0, readiness_ratio))))
    result.impact_level = level_for_score(result.impact)
    result.readiness_level = level_for_score(result.readiness)
    result.systemic = list(extra_systemic) + detect_systemic_risks(cluster_list)
    if broken:
        pass  # (kept for readability: the feature rows above carry the detail)
    _add_reasons(result, profile, per_feature_contrib, per_feature_meta)
    return result


def _product(values: Iterable[float]) -> float:
    product = 1.0
    for value in values:
        product *= 1.0 - max(0.0, min(1.0, value))
    return product


def _feature_rows(
    profile: UsageProfile,
    contributions: dict[str, float],
    meta: dict[str, dict[str, Any]],
) -> list[FeatureImpact]:
    """One row per feature worth showing: touched features + active ones."""
    keys = set(meta) | {key for key in profile.active_keys()}
    rows: list[FeatureImpact] = []
    for key in sorted(keys):
        weight = profile.weight(key)
        info = meta.get(key, {})
        severity = str(info.get("severity", ""))
        confidence = str(info.get("confidence", ""))
        unavailable = bool(info.get("unavailable", False))
        if not info:
            status = FEATURE_OK if weight > 0 else FEATURE_UNUSED
        elif weight <= 0:
            status = FEATURE_UNUSED
        elif severity in {SEVERITY_CRITICAL, SEVERITY_HIGH} and confidence == CONFIDENCE_HIGH and unavailable:
            status = FEATURE_FAIL
        else:
            status = FEATURE_WARN
        total = contributions.get(key, 0.0)
        rows.append(
            FeatureImpact(
                key=key,
                level=profile.level(key),
                weight=weight,
                status=status,
                severity=severity,
                confidence=confidence,
                cluster_keys=list(info.get("clusters", [])),
                personal=min(1.0, total) * weight,
                readiness_loss=(min(1.0, total) ** READINESS_DAMPING) * weight,
                unavailable=unavailable,
            )
        )
    rows.sort(key=lambda row: (-row.weight, -row.personal, row.key))
    return rows


def _add_reasons(
    result: PersonalReadiness,
    profile: UsageProfile,
    contributions: dict[str, float],
    meta: dict[str, dict[str, Any]],
) -> None:
    for key, info in sorted(meta.items(), key=lambda item: -item[1].get("_rank", 0)):
        level = profile.level(key)
        severity = info.get("severity", "")
        confidence = info.get("confidence", "")
        label_zh = profile.label(key, lang="zh")
        label_en = profile.label(key, lang="en")
        if level == LEVEL_UNUSED:
            result.reasons_zh.append(f"{label_zh}：{severity} / 可信度 {confidence} —— 你的画像标记为 unused，不计入个人影响")
            result.reasons_en.append(
                f"{label_en}: {severity} / confidence {confidence} - marked unused in your profile, not counted"
            )
        else:
            result.reasons_zh.append(f"{label_zh}：{severity} / 可信度 {confidence}（{LEVEL_WEIGHTS.get(level, 0):.2f} 权重）")
            result.reasons_en.append(
                f"{label_en}: {severity} / confidence {confidence} (weight {LEVEL_WEIGHTS.get(level, 0):.2f})"
            )
