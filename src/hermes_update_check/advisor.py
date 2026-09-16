"""The advisor: turn provenance + risk + gates into one explainable verdict.

Priority (fixed, so the answer is reproducible and arguable):

1. no update to make            (up to date / already ahead of stable)
2. insufficient data            (never dress missing data up as "safe")
3. abnormal local environment
4. hard gates                   (this is what overrides a *low* score)
5. risk score                   (AVOID >= 81, WAIT > threshold)
6. release cooling period       (soft minimum_release_age_days)
7. UPDATE
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .clusters import CONFIDENCE_HIGH, SEVERITY_CRITICAL, SEVERITY_HIGH, RegressionCluster
from .config import Config
from .gates import GateReport
from .github_api import Release
from .provenance import (
    UPDATE_STATUS_AHEAD,
    UPDATE_STATUS_MANUAL_REVIEW,
    UPDATE_STATUS_UNKNOWN,
    UPDATE_STATUS_UP_TO_DATE,
    CodeProvenance,
    UpdateDecision,
)
from .risk import (
    RECOMMEND_AVOID,
    RECOMMEND_UNKNOWN,
    RECOMMEND_UP_TO_DATE,
    RECOMMEND_UPDATE,
    RECOMMEND_WAIT,
    RiskAssessment,
)
from .util import hours_between, iso, utcnow

#: Extra actions this layer can return (superset of risk.RECOMMEND_*).
RECOMMEND_AHEAD_OF_STABLE = "AHEAD_OF_STABLE"
RECOMMEND_MANUAL_REVIEW = "MANUAL_REVIEW"

DECIDED_BY = {
    "update_status": "更新状态",
    "insufficient_data": "数据不足",
    "environment": "本地环境异常",
    "hard_gate": "Hard Gate",
    "risk_score": "风险评分",
    "cooling_period": "观察期",
    "ok": "全部通过",
}

RECHECK_CRITICAL_HOURS = 12.0
RECHECK_SEVERE_HOURS = 24.0
RECHECK_ROUTINE_HOURS = 24.0
RECHECK_WAIT_DAYS = 3.0
RECHECK_AVOID_DAYS = 7.0


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
    headline_zh: str = ""
    headline_en: str = ""
    reasons_zh: list[str] = field(default_factory=list)
    reasons_en: list[str] = field(default_factory=list)
    remediation_zh: list[str] = field(default_factory=list)
    remediation_en: list[str] = field(default_factory=list)
    blocking_gates: list[str] = field(default_factory=list)
    recheck_at: Optional[datetime] = None
    recheck_hours: Optional[float] = None
    recheck_reason_zh: str = ""
    recheck_reason_en: str = ""
    score_is_lower_bound: bool = False

    @property
    def is_blocking(self) -> bool:
        return self.action in {RECOMMEND_WAIT, RECOMMEND_AVOID, RECOMMEND_UNKNOWN, RECOMMEND_MANUAL_REVIEW}

    @property
    def decided_by_label(self) -> str:
        return DECIDED_BY.get(self.decided_by, self.decided_by)

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "decided_by": self.decided_by,
            "level": self.level,
            "overall": self.overall,
            "stability": self.stability,
            "change_risk": self.change_risk,
            "regression_signal": self.regression_signal,
            "data_confidence": self.data_confidence,
            "score_is_lower_bound": self.score_is_lower_bound,
            "blocking_gates": self.blocking_gates,
            "reasons_zh": self.reasons_zh,
            "reasons_en": self.reasons_en,
            "remediation_zh": self.remediation_zh,
            "remediation_en": self.remediation_en,
            "recheck_at": iso(self.recheck_at),
            "recheck_hours": round(self.recheck_hours, 2) if self.recheck_hours is not None else None,
            "recheck_reason_zh": self.recheck_reason_zh,
            "recheck_reason_en": self.recheck_reason_en,
        }


def advise(
    cfg: Config,
    *,
    provenance: CodeProvenance,
    decision: UpdateDecision,
    assessment: Optional[RiskAssessment],
    gates: GateReport,
    clusters: list[RegressionCluster] = (),  # type: ignore[assignment]
    release: Optional[Release] = None,
    now: Optional[datetime] = None,
) -> Recommendation:
    """Apply the priority order and produce the final recommendation."""
    moment = now or utcnow()
    rec = Recommendation(
        action=RECOMMEND_UPDATE,
        decided_by="ok",
        level=assessment.level if assessment else "UNKNOWN",
        overall=assessment.overall if assessment else None,
        stability=assessment.stability if assessment else None,
        change_risk=assessment.change_risk if assessment else None,
        regression_signal=assessment.regression_signal if assessment else None,
        data_confidence=assessment.data_confidence if assessment else None,
        score_is_lower_bound=bool(assessment.score_is_lower_bound) if assessment else False,
    )
    if assessment is not None:
        rec.reasons_zh.extend(assessment.summary_zh[:6])
        rec.reasons_en.extend(assessment.summary_en[:6])

    # open CRITICAL clusters are what pull the recommended recheck earlier; the
    # per-cluster grading itself already feeds the regression signal
    active_critical = [c for c in clusters if c.severity == SEVERITY_CRITICAL and c.open_count > 0]

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
            routine_hours=RECHECK_SEVERE_HOURS if active_critical else RECHECK_ROUTINE_HOURS,
        )

    # -- 3. environment ------------------------------------------------------ #
    if gates.environment_abnormal:
        env_gate = next((g for g in gates.gates if g.key == "environment"), None)
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
        )

    # -- 4. hard gates ------------------------------------------------------- #
    blocking = gates.blocking
    if blocking:
        rec.blocking_gates = [g.key for g in blocking]
        gate_reasons_zh: list[str] = []
        gate_reasons_en: list[str] = []
        gate_remediation_zh: list[str] = []
        gate_remediation_en: list[str] = []
        for gate in blocking:
            gate_reasons_zh.append(gate.reason_zh)
            gate_reasons_en.append(gate.reason_en)
            if gate.remediation_zh:
                gate_remediation_zh.append(gate.remediation_zh)
                gate_remediation_en.append(gate.remediation_en)
        # the deciding reasons come first: they are what the user must act on
        rec.reasons_zh = [r for r in gate_reasons_zh if r] + rec.reasons_zh
        rec.reasons_en = [r for r in gate_reasons_en if r] + rec.reasons_en
        rec.remediation_zh = gate_remediation_zh + rec.remediation_zh
        rec.remediation_en = gate_remediation_en + rec.remediation_en
        return _finalize(
            rec,
            action=RECOMMEND_WAIT,
            decided_by="hard_gate",
            headline_zh="HARD GATE TRIGGERED —— WAIT",
            headline_en="HARD GATE TRIGGERED - WAIT",
            moment=moment,
            clusters=clusters,
            gate_deadline=gates.earliest_recheck,
        )

    # -- 5. risk score ------------------------------------------------------- #
    if assessment is not None and assessment.score is not None:
        if assessment.score >= 81:
            rec.reasons_zh.append(f"风险评分 {assessment.score} 进入最高区间（>=81）")
            rec.reasons_en.append(f"risk score {assessment.score} is in the top band (>= 81)")
            return _finalize(
                rec,
                action=RECOMMEND_AVOID,
                decided_by="risk_score",
                headline_zh="强烈不建议更新",
                headline_en="strongly not recommended",
                moment=moment,
                clusters=clusters,
                routine_hours=RECHECK_AVOID_DAYS * 24,
            )
        if assessment.score > cfg.risk_threshold:
            rec.reasons_zh.append(f"风险评分 {assessment.score} 高于阈值 {cfg.risk_threshold}")
            rec.reasons_en.append(f"risk score {assessment.score} is above the threshold {cfg.risk_threshold}")
            return _finalize(
                rec,
                action=RECOMMEND_WAIT,
                decided_by="risk_score",
                headline_zh="暂停更新，继续观察",
                headline_en="hold the update, keep observing",
                moment=moment,
                clusters=clusters,
                routine_hours=RECHECK_WAIT_DAYS * 24,
            )

    # -- 6. cooling period --------------------------------------------------- #
    age_days = assessment.age_days if assessment and assessment.age_days is not None else None
    if age_days is None and release is not None:
        age_days = release.age_days
    if age_days is not None and age_days < cfg.minimum_release_age_days:
        remaining_hours = max(1.0, (cfg.minimum_release_age_days - age_days) * 24)
        rec.reasons_zh.append(f"发布仅 {age_days:.1f} 天，低于观察期 {cfg.minimum_release_age_days:g} 天")
        rec.reasons_en.append(
            f"release is only {age_days:.1f} days old (observation window {cfg.minimum_release_age_days:g} days)"
        )
        deadline = moment + timedelta(hours=remaining_hours)
        if release is not None and release.when is not None:
            candidate = release.when + timedelta(days=cfg.minimum_release_age_days)
            deadline = min(deadline, candidate) if candidate > moment else deadline
        return _finalize(
            rec,
            action=RECOMMEND_WAIT,
            decided_by="cooling_period",
            headline_zh="观察期未满，建议暂缓",
            headline_en="still inside the observation window",
            moment=moment,
            clusters=clusters,
            gate_deadline=deadline,
        )

    # -- 7. update ----------------------------------------------------------- #
    rec.remediation_zh.append("更新前请先备份：hermes-update-check update（默认会执行 hermes update --backup）")
    rec.remediation_en.append("back up first: hermes-update-check update (runs hermes update --backup by default)")
    return _finalize(
        rec,
        action=RECOMMEND_UPDATE,
        decided_by="ok",
        headline_zh="风险可接受，可以更新（先备份）",
        headline_en="risk is acceptable - you may update (back up first)",
        moment=moment,
        clusters=clusters,
        routine_hours=RECHECK_ROUTINE_HOURS,
    )


def _finalize(
    rec: Recommendation,
    *,
    action: str,
    decided_by: str,
    headline_zh: str,
    headline_en: str,
    moment: datetime,
    clusters: list[RegressionCluster],
    gate_deadline: Optional[datetime] = None,
    routine_hours: float = RECHECK_ROUTINE_HOURS,
) -> Recommendation:
    rec.action = action
    rec.decided_by = decided_by
    rec.headline_zh = headline_zh
    rec.headline_en = headline_en

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
    return rec


def recommended_recheck(
    *,
    action: str,
    gate_deadline: Optional[datetime],
    clusters: list[RegressionCluster],
    routine_hours: float,
    now: datetime,
) -> tuple[Optional[float], str, str]:
    """How soon is it worth looking again?

    A hard-gate deadline is *the* moment the verdict can change (the release has
    matured), so it wins; an open CRITICAL regression can change faster than
    that, so the sooner of the two is used. Without either, the routine interval
    applies.
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

    if gate_deadline is not None:
        gate_hours = max(1.0, hours_between(now, gate_deadline))
        # The gate deadline already covers the "wait for the release to mature"
        # horizon, so the routine interval does not compete with it.
        candidates = [
            *urgent,
            (
                gate_hours,
                "Hard Gate 最早可重新评估时间",
                "earliest hard-gate re-evaluation time",
            ),
        ]
    else:
        candidates = list(urgent)
        candidates.append((max(1.0, routine_hours), "常规复查间隔", "routine interval"))

    hours, reason_zh, reason_en = min(candidates, key=lambda item: item[0])
    return round(hours, 1), reason_zh, reason_en
