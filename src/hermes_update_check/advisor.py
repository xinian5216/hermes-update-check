"""The advisor: turn provenance + risk + readiness + gates into one verdict.

Phase 3 inverts the old priority. The question is no longer "is this release
globally safe" (there is no such thing for a project that ships hundreds of commits
a day) but "is this release usable *for the way I use Hermes*".

Priority (fixed, so the answer is reproducible and arguable):

1. nothing to do                    (up to date / already ahead of stable)
2. insufficient data                (never dress missing data up as "safe")
3. abnormal local environment       (fix the machine, not the release)
4. blocking gates                   - systemic critical risk, confirmed critical-workflow
                                      regression, broken rollback path, dirty worktree,
                                      and a release younger than the policy window
5. a *suspect* critical workflow     (severity HIGH, confidence MEDIUM) -> WAIT
6. Core Feature Readiness           low -> WAIT · acceptable -> ACCEPTABLE · high -> SAFE
7. Global risk                      *background only*: it can cap SAFE down to ACCEPTABLE,
                                      it never overrides the readiness verdict

Everything a user sees comes with the reason that produced it: which gate, which
feature, which cluster, which weight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from .clusters import CONFIDENCE_HIGH, SEVERITY_CRITICAL, SEVERITY_HIGH, RegressionCluster
from .config import Config
from .gates import GateReport
from .github_api import Release
from .impact import (
    READINESS_ACCEPTABLE_MIN,
    READINESS_SAFE_MIN,
    PersonalReadiness,
)
from .provenance import (
    UPDATE_STATUS_AHEAD,
    UPDATE_STATUS_MANUAL_REVIEW,
    UPDATE_STATUS_UNKNOWN,
    UPDATE_STATUS_UP_TO_DATE,
    CodeProvenance,
    UpdateDecision,
)
from .risk import (
    RECOMMEND_UNKNOWN,
    RECOMMEND_UP_TO_DATE,
    RiskAssessment,
)
from .util import hours_between, iso, utcnow

#: The phase-3 verdicts.
RECOMMEND_BLOCKED = "BLOCKED"
RECOMMEND_WAIT = "WAIT"
RECOMMEND_ACCEPTABLE = "ACCEPTABLE"
RECOMMEND_SAFE = "SAFE"

#: Extra states this layer can return.
RECOMMEND_AHEAD_OF_STABLE = "AHEAD_OF_STABLE"
RECOMMEND_MANUAL_REVIEW = "MANUAL_REVIEW"

#: Backwards-compatible aliases (phase 2 names for the same ideas).
RECOMMEND_UPDATE = RECOMMEND_SAFE
RECOMMEND_AVOID = RECOMMEND_BLOCKED

ALL_ACTIONS = (
    RECOMMEND_BLOCKED,
    RECOMMEND_WAIT,
    RECOMMEND_ACCEPTABLE,
    RECOMMEND_SAFE,
    RECOMMEND_AHEAD_OF_STABLE,
    RECOMMEND_MANUAL_REVIEW,
    RECOMMEND_UNKNOWN,
    RECOMMEND_UP_TO_DATE,
)

DECIDED_BY = {
    "update_status": "更新状态",
    "insufficient_data": "数据不足",
    "environment": "本地环境异常",
    "systemic_risk": "系统级风险",
    "critical_workflow": "关键工作流",
    "rollback_safety": "回滚路径",
    "dirty_worktree": "本地工作区",
    "release_age_policy": "发布年龄策略",
    "readiness": "核心功能可用性",
    "wait_evidence": "关键功能证据不足",
    "caution": "谨慎提示",
    "global_risk": "全局风险（仅背景）",
    "ok": "全部通过",
}

RECHECK_CRITICAL_HOURS = 12.0
RECHECK_SEVERE_HOURS = 24.0
RECHECK_ROUTINE_HOURS = 24.0
RECHECK_WAIT_HOURS = 72.0

#: Readiness bands (mirrors impact.READINESS_* so the report and the verdict agree).
READINESS_BAND_SAFE = READINESS_SAFE_MIN
READINESS_BAND_ACCEPTABLE = READINESS_ACCEPTABLE_MIN


@dataclass
class Recommendation:
    """The final, human-readable verdict."""

    action: str
    decided_by: str
    level: str = "UNKNOWN"
    overall: Optional[int] = None
    stability: Optional[int] = None
    change_risk: Optional[int] = None
    regression_signal: Optional[int] = None
    data_confidence: Optional[int] = None
    environment_risk: Optional[int] = None
    #: phase 3
    personal_impact: Optional[int] = None
    personal_impact_level: str = "UNKNOWN"
    core_readiness: Optional[int] = None
    core_readiness_level: str = "UNKNOWN"
    systemic_risks: list[dict[str, Any]] = field(default_factory=list)
    cautions_zh: list[str] = field(default_factory=list)
    cautions_en: list[str] = field(default_factory=list)
    feature_lines_zh: list[str] = field(default_factory=list)
    feature_lines_en: list[str] = field(default_factory=list)
    headline_zh: str = ""
    headline_en: str = ""
    explanation_zh: str = ""
    explanation_en: str = ""
    reasons_zh: list[str] = field(default_factory=list)
    reasons_en: list[str] = field(default_factory=list)
    remediation_zh: list[str] = field(default_factory=list)
    remediation_en: list[str] = field(default_factory=list)
    blocking_gates: list[str] = field(default_factory=list)
    warning_gates: list[str] = field(default_factory=list)
    #: next routine observation (NOT a promise that updating becomes possible)
    recheck_at: Optional[datetime] = None
    recheck_hours: Optional[float] = None
    recheck_reason_zh: str = ""
    recheck_reason_en: str = ""
    #: when the release-age policy stops blocking / stops warning
    policy_clearance_at: Optional[datetime] = None
    policy_clearance_hours: Optional[float] = None
    score_is_lower_bound: bool = False

    @property
    def is_blocking(self) -> bool:
        return self.action in {RECOMMEND_BLOCKED, RECOMMEND_WAIT, RECOMMEND_UNKNOWN, RECOMMEND_MANUAL_REVIEW}

    @property
    def is_update_friendly(self) -> bool:
        return self.action in {RECOMMEND_SAFE, RECOMMEND_ACCEPTABLE}

    @property
    def decided_by_label(self) -> str:
        return DECIDED_BY.get(self.decided_by, self.decided_by)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "decided_by": self.decided_by,
            "level": self.level,
            "overall": self.overall,
            "stability": self.stability,
            "change_risk": self.change_risk,
            "regression_signal": self.regression_signal,
            "data_confidence": self.data_confidence,
            "environment_risk": self.environment_risk,
            "personal_impact": self.personal_impact,
            "personal_impact_level": self.personal_impact_level,
            "core_readiness": self.core_readiness,
            "core_readiness_level": self.core_readiness_level,
            "systemic_risks": self.systemic_risks,
            "cautions_zh": self.cautions_zh,
            "cautions_en": self.cautions_en,
            "score_is_lower_bound": self.score_is_lower_bound,
            "blocking_gates": self.blocking_gates,
            "warning_gates": self.warning_gates,
            "headline_zh": self.headline_zh,
            "headline_en": self.headline_en,
            "explanation_zh": self.explanation_zh,
            "explanation_en": self.explanation_en,
            "reasons_zh": self.reasons_zh,
            "reasons_en": self.reasons_en,
            "remediation_zh": self.remediation_zh,
            "remediation_en": self.remediation_en,
            "recheck_at": iso(self.recheck_at),
            "recheck_hours": round(self.recheck_hours, 2) if self.recheck_hours is not None else None,
            "recheck_reason_zh": self.recheck_reason_zh,
            "recheck_reason_en": self.recheck_reason_en,
            "policy_clearance_at": iso(self.policy_clearance_at),
            "policy_clearance_hours": (
                round(self.policy_clearance_hours, 2) if self.policy_clearance_hours is not None else None
            ),
        }


def advise(
    cfg: Config,
    *,
    provenance: CodeProvenance,
    decision: UpdateDecision,
    assessment: Optional[RiskAssessment],
    gates: GateReport,
    clusters: Sequence[RegressionCluster] = (),
    release: Optional[Release] = None,
    readiness: Optional[PersonalReadiness] = None,
    now: Optional[datetime] = None,
) -> Recommendation:
    """Apply the phase-3 priority order and produce the final recommendation."""
    moment = now or utcnow()
    rec = Recommendation(
        action=RECOMMEND_SAFE,
        decided_by="ok",
        level=assessment.level if assessment else "UNKNOWN",
        overall=assessment.overall if assessment else None,
        stability=assessment.stability if assessment else None,
        change_risk=assessment.change_risk if assessment else None,
        regression_signal=assessment.regression_signal if assessment else None,
        data_confidence=assessment.data_confidence if assessment else None,
        environment_risk=assessment.environment_risk if assessment else None,
        score_is_lower_bound=bool(assessment.score_is_lower_bound) if assessment else False,
    )
    if readiness is not None:
        rec.personal_impact = readiness.impact
        rec.personal_impact_level = readiness.impact_level
        rec.core_readiness = readiness.readiness
        rec.core_readiness_level = readiness.readiness_level
        rec.systemic_risks = [risk.to_dict() for risk in readiness.systemic if risk.detected]
        rec.feature_lines_zh = _feature_lines(readiness, lang="zh")
        rec.feature_lines_en = _feature_lines(readiness, lang="en")
        rec.reasons_zh.extend(readiness.reasons_zh[:6])
        rec.reasons_en.extend(readiness.reasons_en[:6])
    for gate in gates.warnings:
        reason_zh = gate.reason_zh or gate.name_zh
        reason_en = gate.reason_en or gate.name_en
        if gate.key in {"main_branch", "prerelease", "channel_mismatch", "release_age"}:
            rec.cautions_zh.append(reason_zh)
            rec.cautions_en.append(reason_en)

    # -- 1. is there anything to do at all? --------------------------------- #
    if decision.status == UPDATE_STATUS_UP_TO_DATE:
        return _finalize(
            rec,
            action=RECOMMEND_UP_TO_DATE,
            decided_by="update_status",
            headline_zh="已是最新正式版本，无需操作",
            headline_en="already on the latest stable release - nothing to do",
            moment=moment,
            clusters=clusters,
            routine_hours=RECHECK_ROUTINE_HOURS,
            gates=gates,
        )
    if decision.status == UPDATE_STATUS_AHEAD:
        rec.reasons_zh.append(decision.message_zh)
        rec.reasons_en.append(decision.message_en)
        rec.remediation_zh.append("如果希望回到正式 Release：先备份，再手动 checkout 对应 tag（本工具不会自动降级）")
        rec.remediation_en.append(
            "to move back to a stable release: back up first, then check out the tag manually (no automatic downgrade)"
        )
        if gates.channel_warning:
            rec.reasons_zh.append(gates.channel_warning[0])
            rec.reasons_en.append(gates.channel_warning[1])
        return _finalize(
            rec,
            action=RECOMMEND_AHEAD_OF_STABLE,
            decided_by="update_status",
            headline_zh="当前代码已领先最新正式版本（开发分支模式）",
            headline_en="current code is ahead of the latest stable release (development channel mode)",
            moment=moment,
            clusters=clusters,
            routine_hours=RECHECK_ROUTINE_HOURS,
            gates=gates,
        )
    if decision.status == UPDATE_STATUS_MANUAL_REVIEW:
        rec.reasons_zh.append(decision.message_zh)
        rec.reasons_en.append(decision.message_en)
        return _finalize(
            rec,
            action=RECOMMEND_MANUAL_REVIEW,
            decided_by="update_status",
            headline_zh="需要人工判断（非标准安装状态）",
            headline_en="manual review required (non-standard install state)",
            moment=moment,
            clusters=clusters,
            routine_hours=RECHECK_ROUTINE_HOURS,
            gates=gates,
        )

    # -- 2. insufficient data ------------------------------------------------ #
    if decision.status == UPDATE_STATUS_UNKNOWN or (assessment is not None and assessment.insufficient_data):
        rec.reasons_zh.append("观测数据不足：不能把缺失数据当作安全（missing information != safe）")
        rec.reasons_en.append("insufficient observation data: missing information is never treated as safe")
        return _finalize(
            rec,
            action=RECOMMEND_UNKNOWN,
            decided_by="insufficient_data",
            headline_zh="WAIT —— 观测数据不足",
            headline_en="WAIT - insufficient observation data",
            moment=moment,
            clusters=clusters,
            routine_hours=RECHECK_ROUTINE_HOURS,
            gates=gates,
        )

    # -- 3. environment ------------------------------------------------------ #
    if gates.environment_abnormal:
        env_gate = gates.get("environment")
        if env_gate is not None:
            rec.reasons_zh.append(env_gate.reason_zh)
            rec.reasons_en.append(env_gate.reason_en)
            if env_gate.remediation_zh:
                rec.remediation_zh.append(env_gate.remediation_zh)
                rec.remediation_en.append(env_gate.remediation_en)
        return _finalize(
            rec,
            action=RECOMMEND_MANUAL_REVIEW,
            decided_by="environment",
            headline_zh="先修复本地环境，暂不更新",
            headline_en="fix the local environment before updating",
            moment=moment,
            clusters=clusters,
            routine_hours=RECHECK_ROUTINE_HOURS,
            gates=gates,
        )

    # -- 4. blocking gates --------------------------------------------------- #
    blocking = gates.blocking
    if blocking:
        rec.blocking_gates = [gate.key for gate in blocking]
        gate_reasons_zh: list[str] = []
        gate_reasons_en: list[str] = []
        gate_remediation_zh: list[str] = []
        gate_remediation_en: list[str] = []
        for gate in blocking:
            gate_reasons_zh.append(gate.reason_zh or gate.name_zh)
            gate_reasons_en.append(gate.reason_en or gate.name_en)
            if gate.remediation_zh:
                gate_remediation_zh.append(gate.remediation_zh)
                gate_remediation_en.append(gate.remediation_en)
        rec.reasons_zh = gate_reasons_zh + rec.reasons_zh
        rec.reasons_en = gate_reasons_en + rec.reasons_en
        rec.remediation_zh = gate_remediation_zh + rec.remediation_zh
        rec.remediation_en = gate_remediation_en + rec.remediation_en

        priority = (
            "systemic_risk",
            "critical_workflow",
            "rollback_safety",
            "dirty_worktree",
            "release_age",
            "environment",
        )
        decided_by = next(
            (key for key in priority if any(gate.key == key for gate in blocking)),
            blocking[0].key,
        )
        label = {
            "systemic_risk": ("systemic_risk", "BLOCKED —— 系统级风险", "BLOCKED - systemic critical risk"),
            "critical_workflow": (
                "critical_workflow",
                "BLOCKED —— 关键工作流不可用",
                "BLOCKED - a critical workflow is broken",
            ),
            "rollback_safety": (
                "rollback_safety",
                "BLOCKED —— 回滚路径不可用",
                "BLOCKED - the rollback path is not available",
            ),
            "dirty_worktree": (
                "dirty_worktree",
                "BLOCKED —— 本地工作区不安全",
                "BLOCKED - the local worktree is not safe",
            ),
            "release_age": (
                "release_age_policy",
                "BLOCKED —— 发布太新（策略阻断）",
                "BLOCKED - the release is too fresh (policy)",
            ),
            "insufficient_data": ("insufficient_data", "WAIT —— 观测数据不足", "WAIT - insufficient observation data"),
            "environment": ("environment", "先修复本地环境", "fix the local environment first"),
        }[decided_by]
        return _finalize(
            rec,
            action=RECOMMEND_BLOCKED if decided_by != "insufficient_data" else RECOMMEND_UNKNOWN,
            decided_by=label[0],
            headline_zh=label[1],
            headline_en=label[2],
            moment=moment,
            clusters=clusters,
            gate_deadline=gates.earliest_recheck,
            gates=gates,
        )

    # -- 5. critical workflow: severe but not yet confirmed ------------------ #
    suspect = gates.get("critical_workflow")
    if suspect is not None and suspect.status == "WARN" and readiness is not None and readiness.critical_suspect:
        rec.reasons_zh = [suspect.reason_zh, *rec.reasons_zh]
        rec.reasons_en = [suspect.reason_en, *rec.reasons_en]
        rec.remediation_zh.append("等这些报告被确认或关闭，或把该功能在 usage_profile 中下调为 important/optional")
        rec.remediation_en.append(
            "wait for those reports to be confirmed or closed, or downgrade the feature in usage_profile"
        )
        return _finalize(
            rec,
            action=RECOMMEND_WAIT,
            decided_by="wait_evidence",
            headline_zh="WAIT —— 关键功能存在未确认的严重报告",
            headline_en="WAIT - unconfirmed severe reports on a critical workflow",
            moment=moment,
            clusters=clusters,
            routine_hours=RECHECK_WAIT_HOURS,
            gate_deadline=gates.earliest_recheck,
            gates=gates,
        )

    # -- 6. core feature readiness drives the verdict ------------------------ #
    readiness_value = readiness.readiness if readiness is not None else None
    if readiness_value is not None and readiness_value < READINESS_BAND_ACCEPTABLE:
        rec.reasons_zh.insert(0, f"核心功能可用性 {readiness_value}/100 低于可接受下限 {READINESS_BAND_ACCEPTABLE}")
        rec.reasons_en.insert(
            0, f"core feature readiness {readiness_value}/100 is below the acceptable floor {READINESS_BAND_ACCEPTABLE}"
        )
        return _finalize(
            rec,
            action=RECOMMEND_WAIT,
            decided_by="readiness",
            headline_zh="WAIT —— 核心功能可用性偏低",
            headline_en="WAIT - core feature readiness is low",
            moment=moment,
            clusters=clusters,
            routine_hours=RECHECK_WAIT_HOURS,
            gates=gates,
        )

    cautions_zh = list(rec.cautions_zh)
    cautions_en = list(rec.cautions_en)

    # The phase-2 "cooling period" no longer blocks; if the user still sets
    # minimum_release_age_days it only caps the verdict at ACCEPTABLE (doc section 9).
    age_days = assessment.age_days if assessment is not None and assessment.age_days is not None else None
    if age_days is None and release is not None:
        age_days = release.age_days
    if cfg.minimum_release_age_days and age_days is not None and age_days < cfg.minimum_release_age_days:
        cautions_zh.append(
            f"发布仅 {age_days:.1f} 天，低于你配置的观察期 {cfg.minimum_release_age_days:g} 天"
            "（现在只限制最高等级，不再阻断更新）"
        )
        cautions_en.append(
            f"release is only {age_days:.1f} days old, inside your configured observation window "
            f"{cfg.minimum_release_age_days:g} days (it now caps the verdict instead of blocking)"
        )
    global_risk_note = ""
    if assessment is not None and assessment.score is not None and assessment.score > cfg.risk_threshold:
        global_risk_note = (
            f"全局风险 {assessment.score} 高于阈值 {cfg.risk_threshold}（仅作背景，不再单独决定更新与否）"
        )
        cautions_zh.append(global_risk_note)
        cautions_en.append(
            f"global risk {assessment.score} is above the threshold {cfg.risk_threshold} "
            "(background information; it no longer decides on its own)"
        )

    # Without a profile (legacy call path) fall back to the global score: a low
    # global risk still means SAFE, a high one still caps at ACCEPTABLE.
    if readiness_value is None:
        global_ok = assessment is None or assessment.score is None or assessment.score <= cfg.risk_threshold
        high_readiness = global_ok
    else:
        high_readiness = readiness_value >= READINESS_BAND_SAFE

    if high_readiness and not cautions_zh:
        rec.remediation_zh.append("更新前请先备份：hermes-update-check update（默认会执行 hermes update --backup）")
        rec.remediation_en.append("back up first: hermes-update-check update (runs hermes update --backup by default)")
        return _finalize(
            rec,
            action=RECOMMEND_SAFE,
            decided_by="ok",
            headline_zh="SAFE —— 没有明显风险，可以更新",
            headline_en="SAFE - no meaningful risk found, you may update",
            moment=moment,
            clusters=clusters,
            routine_hours=RECHECK_ROUTINE_HOURS,
            gates=gates,
        )

    if cautions_zh:
        rec.cautions_zh = cautions_zh
        rec.cautions_en = cautions_en
        decided_by = "global_risk" if global_risk_note else "caution"
    else:
        decided_by = "caution"
    rec.remediation_zh.append("更新前请先备份：hermes-update-check update（默认会执行 hermes update --backup）")
    rec.remediation_en.append("back up first: hermes-update-check update (runs hermes update --backup by default)")
    return _finalize(
        rec,
        action=RECOMMEND_ACCEPTABLE,
        decided_by=decided_by,
        headline_zh="ACCEPTABLE —— 可以更新（先备份）",
        headline_en="ACCEPTABLE - you may update (back up first)",
        moment=moment,
        clusters=clusters,
        routine_hours=RECHECK_ROUTINE_HOURS,
        gates=gates,
    )


def _feature_lines(readiness: PersonalReadiness, *, lang: str) -> list[str]:
    """The "YOUR CORE FEATURES" table rows: one line per feature worth showing."""
    lines: list[str] = []
    for feature in readiness.features:
        if feature.is_unused and not feature.cluster_keys:
            continue
        label = readiness.profile.label(feature.key, lang=lang)
        suffix = f" ×{feature.level}" if lang == "en" else f"（{feature.level}）"
        lines.append(f"{label:<28} {feature.status}{suffix}")
    return lines[:14]


def _finalize(
    rec: Recommendation,
    *,
    action: str,
    decided_by: str,
    headline_zh: str,
    headline_en: str,
    moment: datetime,
    clusters: Sequence[RegressionCluster],
    gates: GateReport,
    gate_deadline: Optional[datetime] = None,
    routine_hours: float = RECHECK_ROUTINE_HOURS,
) -> Recommendation:
    rec.action = action
    rec.decided_by = decided_by
    rec.headline_zh = headline_zh
    rec.headline_en = headline_en
    rec.warning_gates = [gate.key for gate in gates.warnings]
    rec.explanation_zh, rec.explanation_en = _explain(action, rec)

    hours, reason_zh, reason_en = recommended_recheck(
        action=action,
        gate_deadline=gate_deadline,
        clusters=clusters,
        routine_hours=routine_hours,
        now=moment,
    )
    rec.recheck_hours = hours
    rec.recheck_reason_zh = reason_zh
    rec.recheck_reason_en = reason_en
    rec.recheck_at = moment + timedelta(hours=hours) if hours is not None else None

    if gates.policy_clearance is not None and action != RECOMMEND_UP_TO_DATE:
        clearance = max(0.0, hours_between(moment, gates.policy_clearance))
        if clearance > 0:
            rec.policy_clearance_hours = round(clearance, 1)
            rec.policy_clearance_at = gates.policy_clearance
    return rec


def _explain(action: str, rec: Recommendation) -> tuple[str, str]:
    """One-sentence, plain-language reading of the verdict (doc section 12)."""
    impact = rec.personal_impact
    readiness = rec.core_readiness
    if action == RECOMMEND_SAFE:
        return (
            "没有发现会影响你工作流的已知问题，风险已经可以接受。",
            "no known issue affects your workflows; the risk is acceptable for you.",
        )
    if action == RECOMMEND_ACCEPTABLE:
        if readiness is not None and impact is not None and readiness >= READINESS_BAND_SAFE:
            return (
                "存在一些谨慎提示（发布较新 / 开发分支），但没有确认影响你的关键工作流：可以更新，建议先备份。",
                "there are cautions (a fresh release / development channel) but nothing confirmed to affect your "
                "critical workflows: you may update - back up first.",
            )
        return (
            f"这个版本包含已知回归，但按你的画像（个人影响 {impact if impact is not None else '?'}/100、"
            f"核心可用性 {readiness if readiness is not None else '?'}/100）没有伤到关键工作流：可以更新，建议先备份。",
            f"this release contains known regressions, but per your profile (personal impact "
            f"{impact if impact is not None else '?'}/100, core readiness {readiness if readiness is not None else '?'}/100) "
            "they do not hit your critical workflows: you may update - back up first.",
        )
    if action == RECOMMEND_WAIT:
        return (
            "证据还不充分或关键功能存在可疑回归：先观察，等复查时间再看。",
            "the evidence is not conclusive yet, or a critical workflow looks suspect: observe and re-check later.",
        )
    if action == RECOMMEND_BLOCKED:
        return (
            "存在系统级风险或已确认的关键功能回归：这个版本现在不能更新。",
            "there is a systemic risk or a confirmed regression in a critical workflow: do not update this release yet.",
        )
    if action == RECOMMEND_AHEAD_OF_STABLE:
        return (
            "你当前运行的是开发分支代码，不是 GitHub 上的正式版本。",
            "you are running development-branch code, not the published stable release.",
        )
    if action == RECOMMEND_UNKNOWN:
        return (
            "观测数据不足，无法给出结论：缺失数据不等于安全。",
            "not enough observation data to conclude anything: missing data is not safety.",
        )
    if action == RECOMMEND_MANUAL_REVIEW:
        return (
            "本地环境或安装状态异常，需要人工处理。",
            "the local environment or installation state needs a human decision.",
        )
    return ("", "")


def recommended_recheck(
    *,
    action: str,
    gate_deadline: Optional[datetime],
    clusters: Sequence[RegressionCluster],
    routine_hours: float,
    now: datetime,
) -> tuple[Optional[float], str, str]:
    """Next monitoring check - explicitly *not* a promise that updating becomes possible.

    The earliest-policy-clearance time is reported separately (see
    :attr:`Recommendation.policy_clearance_at`) so "check again in 12 h" is never
    read as "you can update in 12 h".
    """
    urgent: list[tuple[float, str, str]] = []

    critical_open = [c for c in clusters if c.severity == SEVERITY_CRITICAL and c.open_count > 0]
    if critical_open:
        names_zh = "、".join(c.zh for c in critical_open[:3])
        names_en = ", ".join(c.en for c in critical_open[:3])
        urgent.append(
            (
                RECHECK_CRITICAL_HOURS,
                f"存在 open 的 CRITICAL 回归（{names_zh}）：建议 12 小时后复查",
                f"open CRITICAL regression(s) ({names_en}): re-check in 12 h",
            )
        )
    severe_open = [
        c for c in clusters if c.severity == SEVERITY_HIGH and c.open_count > 0 and c.confidence == CONFIDENCE_HIGH
    ]
    if severe_open:
        urgent.append(
            (
                RECHECK_SEVERE_HOURS,
                "存在高可信度的严重回归报告：建议 24 小时后复查",
                "high-confidence severe regression reports: re-check in 24 h",
            )
        )

    candidates = list(urgent)
    if gate_deadline is not None:
        gate_hours = max(1.0, hours_between(now, gate_deadline))
        candidates.append((gate_hours, "阻断条件的解除时间", "when the blocking condition clears"))
    candidates.append((max(1.0, routine_hours), "常规复查间隔", "routine interval"))

    hours, reason_zh, reason_en = min(candidates, key=lambda item: item[0])
    return round(hours, 1), reason_zh, reason_en
