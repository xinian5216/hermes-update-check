"""Gates: the few rules that can still stop an update.

Phase 2 blocked on anything that looked risky (a 48-hour-old release, a main-branch
checkout, any active gateway regression). Phase 3 keeps only what is genuinely
non-negotiable, because a tool that always says WAIT is a tool nobody uses:

**Blocking** (doc section 7)
  1. *systemic critical risk* - data corruption, session loss, credential loss,
     config destruction, installation corruption, rollback failure, cannot start,
     all providers down - and only with high-confidence evidence;
  2. *critical workflow broken* - a regression the profile marks ``critical``,
     confirmed (severity >= HIGH and confidence HIGH);
  3. *update mechanism* - the update/rollback path itself reported broken;
  4. *local safety* - rollback not available, dirty worktree, environment broken;
  5. *insufficient data* - never dress missing data up as safe.

**Demoted to warnings / risk points** (doc sections 8-10)
  release age, main channel, prerelease, channel mismatch, and every ordinary
  gateway / MCP / provider regression. They raise Environment Risk, they cap the
  verdict at ACCEPTABLE and they show up in the report - they no longer stop you.

Release age is segmented instead of a 48 h wall (``release_age_policy``):
``< block_hours`` blocks, the caution band caps at ACCEPTABLE, the rest passes.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from .clusters import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    RegressionCluster,
)
from .config import Config, ReleaseAgePolicy
from .github_api import Release
from .impact import (
    PersonalReadiness,
)
from .local_env import LocalEnv
from .logging_setup import get_logger
from .overrides import OverrideReport
from .provenance import (
    CHANNEL_MAIN,
    CHANNEL_PRERELEASE,
    UPDATE_STATUS_AHEAD,
    UPDATE_STATUS_UP_TO_DATE,
    CodeProvenance,
    UpdateDecision,
    channel_mismatch,
)
from .risk import RiskAssessment
from .rollback_safety import SAFETY_FAIL, RollbackSafety
from .util import iso, utcnow

GATE_BLOCK = "BLOCK"
GATE_WARN = "WARN"
GATE_PASS = "PASS"
GATE_SKIP = "SKIP"

#: Ordinary regression classes that only warn (they feed readiness, not the verdict).
WARNING_REGRESSION_GATES: tuple[tuple[str, str, str, str], ...] = (
    ("GATEWAY", "warn_active_gateway_regression", "Gateway 回归", "active gateway regression"),
    ("MCP", "warn_active_mcp_regression", "MCP 回归", "active MCP regression"),
    ("PROVIDER", "warn_active_provider_regression", "Provider 回归", "active provider regression"),
    ("AUTH", "warn_active_auth_regression", "认证/凭证回归", "active auth regression"),
    ("CRASH", "warn_active_crash_regression", "崩溃报告", "active crash reports"),
)


@dataclass
class EnvironmentState:
    """Cheap, file-level sanity of the local install (no subprocesses)."""

    hermes_home_exists: bool = True
    config_readable: bool = True
    install_kind_known: bool = True
    state_db_readable: bool = True
    problems_zh: list[str] = field(default_factory=list)
    problems_en: list[str] = field(default_factory=list)

    @property
    def abnormal(self) -> bool:
        return bool(self.problems_zh)

    def to_dict(self) -> dict[str, object]:
        return {
            "abnormal": self.abnormal,
            "hermes_home_exists": self.hermes_home_exists,
            "config_readable": self.config_readable,
            "install_kind_known": self.install_kind_known,
            "state_db_readable": self.state_db_readable,
            "problems_zh": self.problems_zh,
            "problems_en": self.problems_en,
        }


def probe_environment(env: LocalEnv, *, logger: Optional[logging.Logger] = None) -> EnvironmentState:
    """Check the local environment without touching the network or spawning work."""
    log = logger or get_logger("env")
    state = EnvironmentState()

    if not env.hermes_home.exists():
        state.hermes_home_exists = False
        state.problems_zh.append(f"HERMES_HOME 不存在：{env.hermes_home}")
        state.problems_en.append(f"HERMES_HOME missing: {env.hermes_home}")

    config_path = env.config_path
    if config_path.exists():
        try:
            config_path.read_bytes()
        except OSError as exc:
            state.config_readable = False
            state.problems_zh.append(f"config.yaml 不可读：{exc}")
            state.problems_en.append(f"config.yaml unreadable: {exc}")

    if env.install_kind == "unknown":
        state.install_kind_known = False
        state.problems_zh.append("无法识别安装方式（git / pip / docker）")
        state.problems_en.append("install method could not be identified (git / pip / docker)")

    db_path = env.state_db
    if db_path.exists():
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
                conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as exc:
            state.state_db_readable = False
            state.problems_zh.append(f"state.db 读取异常：{exc}")
            state.problems_en.append(f"state.db read error: {exc}")
            log.debug("state.db probe failed: %s", exc)

    return state


@dataclass
class GateResult:
    key: str
    name_zh: str
    name_en: str
    status: str
    reason_zh: str = ""
    reason_en: str = ""
    remediation_zh: str = ""
    remediation_en: str = ""
    earliest_recheck: Optional[datetime] = None
    detail_zh: str = ""
    detail_en: str = ""
    #: WARN gates that only *cap* the verdict (ACCEPTABLE, never SAFE)
    caps_verdict: bool = True

    @property
    def blocking(self) -> bool:
        return self.status == GATE_BLOCK

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label_zh": self.name_zh,
            "label_en": self.name_en,
            "status": self.status,
            "reason_zh": self.reason_zh,
            "reason_en": self.reason_en,
            "remediation_zh": self.remediation_zh,
            "remediation_en": self.remediation_en,
            "earliest_recheck": iso(self.earliest_recheck),
            "caps_verdict": self.caps_verdict,
        }


@dataclass
class GateReport:
    enabled: bool = True
    gates: list[GateResult] = field(default_factory=list)
    environment: Optional[EnvironmentState] = None
    channel_warning: Optional[tuple[str, str]] = None
    #: release-age band: block | caution | acceptable | normal ("" when inapplicable)
    age_band: str = ""
    #: when the release leaves the blocking band (Earliest Policy Clearance)
    policy_clearance: Optional[datetime] = None
    #: when the release leaves the caution band
    caution_clearance: Optional[datetime] = None

    @property
    def blocking(self) -> list[GateResult]:
        return [g for g in self.gates if g.status == GATE_BLOCK]

    @property
    def warnings(self) -> list[GateResult]:
        return [g for g in self.gates if g.status == GATE_WARN]

    @property
    def cautions(self) -> list[GateResult]:
        """Warnings that cap the verdict at ACCEPTABLE."""
        return [g for g in self.warnings if g.caps_verdict]

    @property
    def blocked(self) -> bool:
        return bool(self.blocking)

    @property
    def environment_abnormal(self) -> bool:
        return bool(self.environment and self.environment.abnormal)

    def blocked_by(self, key: str) -> bool:
        return any(g.key == key and g.blocking for g in self.gates)

    def get(self, key: str) -> Optional[GateResult]:
        return next((g for g in self.gates if g.key == key), None)

    @property
    def earliest_recheck(self) -> Optional[datetime]:
        stamps = [g.earliest_recheck for g in self.blocking if g.earliest_recheck is not None]
        return min(stamps) if stamps else None

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "blocked": self.blocked,
            "blocking": [g.key for g in self.blocking],
            "warning": [g.key for g in self.warnings],
            "cautions": [g.key for g in self.cautions],
            "age_band": self.age_band,
            "policy_clearance": iso(self.policy_clearance),
            "caution_clearance": iso(self.caution_clearance),
            "gates": [g.to_dict() for g in self.gates],
            "environment": self.environment.to_dict() if self.environment else None,
            "channel_warning_en": self.channel_warning[1] if self.channel_warning else None,
        }


def evaluate_gates(
    cfg: Config,
    *,
    provenance: CodeProvenance,
    decision: UpdateDecision,
    release: Optional[Release],
    assessment: Optional[RiskAssessment],
    clusters: Sequence[RegressionCluster] = (),
    environment: Optional[EnvironmentState] = None,
    readiness: Optional[PersonalReadiness] = None,
    rollback_safety: Optional[RollbackSafety] = None,
    overrides: Optional[OverrideReport] = None,
    now: Optional[datetime] = None,
) -> GateReport:
    """Evaluate every configured gate. Pure function of its inputs (no I/O)."""
    moment = now or utcnow()
    gates_cfg = cfg.hard_gates
    report = GateReport(enabled=bool(gates_cfg.enabled), environment=environment)
    if not gates_cfg.enabled:
        return report

    report.gates.append(_gate_insufficient_data(cfg, gates_cfg, release, assessment, decision))
    report.gates.append(_gate_environment(environment, decision))
    report.gates.append(_gate_release_age(cfg, release, decision, moment, report))
    report.gates.append(_gate_main_branch(gates_cfg, provenance, decision))
    report.gates.append(_gate_dirty_worktree(gates_cfg, provenance, decision, overrides))
    report.gates.append(_gate_prerelease(gates_cfg, provenance, decision))
    report.gates.append(_gate_systemic(gates_cfg, readiness, decision))
    report.gates.append(_gate_critical_workflow(gates_cfg, readiness, decision))
    report.gates.append(_gate_rollback_safety(gates_cfg, rollback_safety, decision))
    for cluster_key, attr, label_zh, label_en in WARNING_REGRESSION_GATES:
        report.gates.append(
            _gate_regression_warning(getattr(gates_cfg, attr, True), cluster_key, label_zh, label_en, clusters)
        )

    report.channel_warning = channel_mismatch(provenance, cfg.preferred_channel)
    if report.channel_warning:
        report.gates.append(
            GateResult(
                key="channel_mismatch",
                name_zh="Channel 与 preferred_channel 不一致",
                name_en="channel differs from preferred_channel",
                status=GATE_WARN,
                reason_zh=report.channel_warning[0],
                reason_en=report.channel_warning[1],
            )
        )
    return report


# --------------------------------------------------------------------------- #
# blocking gates
# --------------------------------------------------------------------------- #


def _gate_insufficient_data(
    cfg: Config,
    gates_cfg,
    release: Optional[Release],
    assessment: Optional[RiskAssessment],
    decision: UpdateDecision,
) -> GateResult:
    result = GateResult(
        key="insufficient_data",
        name_zh="观测数据是否充分",
        name_en="observation data sufficient",
        status=GATE_PASS,
        reason_zh="数据充分",
        reason_en="enough data",
    )
    if not gates_cfg.block_on_insufficient_data:
        result.status = GATE_SKIP
        return result
    if decision.status in {UPDATE_STATUS_AHEAD, UPDATE_STATUS_UP_TO_DATE}:
        result.reason_zh = "无需更新，不适用"
        result.reason_en = "no update to evaluate"
        return result
    if release is None:
        result.status = GATE_BLOCK
        result.reason_zh = "拿不到 Release 信息（GitHub 不可达且无可用缓存）"
        result.reason_en = "no release information (GitHub unreachable and no usable cache)"
        result.remediation_zh = "恢复网络或使用缓存后重试；在此之前不要更新"
        result.remediation_en = "restore network/cache and re-check; do not update before that"
        return result
    if assessment is not None and assessment.insufficient_data:
        result.status = GATE_BLOCK
        result.reason_zh = "风险数据不足（confidence 过低），无法给出可信评估"
        result.reason_en = "insufficient data (confidence too low) for a trustworthy assessment"
        result.remediation_zh = "补齐数据（设置 GITHUB_TOKEN、恢复 API）后重新检查"
        result.remediation_en = "fill in the missing data (set GITHUB_TOKEN, restore the API) and re-check"
    return result


def _gate_environment(environment: Optional[EnvironmentState], decision: UpdateDecision) -> GateResult:
    result = GateResult(
        key="environment",
        name_zh="本地环境状态",
        name_en="local environment state",
        status=GATE_PASS,
        reason_zh="环境正常",
        reason_en="environment OK",
    )
    if environment is not None and environment.abnormal:
        result.status = GATE_BLOCK
        result.reason_zh = "；".join(environment.problems_zh) or "环境异常"
        result.reason_en = "; ".join(environment.problems_en) or "environment abnormal"
        result.remediation_zh = "先修复本地环境（hermes doctor），再考虑更新"
        result.remediation_en = "fix the local environment first (hermes doctor), then revisit the update"
    return result


def _gate_systemic(
    gates_cfg,
    readiness: Optional[PersonalReadiness],
    decision: UpdateDecision,
) -> GateResult:
    """System-wide risks: not negotiable, not profile-dependent (doc section 13)."""
    result = GateResult(
        key="systemic_risk",
        name_zh="系统级风险（数据/Session/凭证/安装/回滚）",
        name_en="systemic critical risk (data/session/credential/install/rollback)",
        status=GATE_PASS,
        reason_zh="未发现系统级风险",
        reason_en="no systemic critical risk detected",
    )
    if readiness is None:
        result.status = GATE_SKIP
        result.reason_zh = "未计算个人可用性，跳过"
        result.reason_en = "personal readiness was not computed"
        return result

    blocking = [risk for risk in readiness.blocking_systemic if risk.source != "local"]
    if blocking and getattr(gates_cfg, "block_on_systemic_risk", True):
        result.status = GATE_BLOCK
        result.reason_zh = "；".join(risk.evidence_zh or risk.zh for risk in blocking[:3])
        result.reason_en = "; ".join(risk.evidence_en or risk.en for risk in blocking[:3])
        result.remediation_zh = "这些是系统级问题（与使用画像无关）：等修复版本或 Issue 关闭后再更新"
        result.remediation_en = (
            "these are system-wide issues independent of your profile: wait for a fix release or closed issues"
        )
        return result

    watched = [risk for risk in readiness.active_systemic if not risk.blocking and risk.source != "local"]
    if watched:
        result.status = GATE_WARN
        result.reason_zh = "；".join(f"{risk.zh}（{risk.confidence or '可信度不足'}）" for risk in watched[:3])
        result.reason_en = "; ".join(f"{risk.en} (confidence {risk.confidence or 'unproven'})" for risk in watched[:3])
    return result


def _gate_critical_workflow(
    gates_cfg,
    readiness: Optional[PersonalReadiness],
    decision: UpdateDecision,
) -> GateResult:
    """A *confirmed* regression in a feature the user marked critical blocks."""
    result = GateResult(
        key="critical_workflow",
        name_zh="关键工作流是否可用",
        name_en="critical workflow available",
        status=GATE_PASS,
        reason_zh="关键功能没有确认的严重回归",
        reason_en="no confirmed severe regression in a critical feature",
    )
    if readiness is None:
        result.status = GATE_SKIP
        result.reason_zh = "未计算个人可用性，跳过"
        result.reason_en = "personal readiness was not computed"
        return result

    broken = [feature for feature in readiness.critical_broken if feature.key not in _SYSTEMIC_FEATURE_KEYS]
    if broken and getattr(gates_cfg, "block_on_critical_workflow", True):
        labels_zh = "、".join(readiness.profile.label(feature.key, lang="zh") for feature in broken[:3])
        labels_en = ", ".join(readiness.profile.label(feature.key, lang="en") for feature in broken[:3])
        result.status = GATE_BLOCK
        result.reason_zh = f"你标记为 critical 的功能出现高可信度严重回归：{labels_zh}"
        result.reason_en = f"high-confidence severe regression in a feature you marked critical: {labels_en}"
        result.remediation_zh = "等这些 Issue 关闭或修复版本发布；如果该功能其实不关键，可调整 usage_profile"
        result.remediation_en = "wait for the issues to close or a fix release; if the feature is not actually critical, adjust usage_profile"
        return result

    suspect = [feature for feature in readiness.critical_suspect if feature.key not in _SYSTEMIC_FEATURE_KEYS]
    if suspect:
        labels_zh = "、".join(readiness.profile.label(feature.key, lang="zh") for feature in suspect[:3])
        labels_en = ", ".join(readiness.profile.label(feature.key, lang="en") for feature in suspect[:3])
        result.status = GATE_WARN
        result.reason_zh = f"关键功能存在尚未确认的严重报告（可信度中等）：{labels_zh}"
        result.reason_en = f"unconfirmed severe reports (medium confidence) for critical features: {labels_en}"
    return result


#: Features whose failures are already covered by the systemic gate.
_SYSTEMIC_FEATURE_KEYS = {"config"}


def _gate_rollback_safety(
    gates_cfg,
    safety: Optional[RollbackSafety],
    decision: UpdateDecision,
) -> GateResult:
    result = GateResult(
        key="rollback_safety",
        name_zh="回滚路径可用",
        name_en="rollback path available",
        status=GATE_PASS,
        reason_zh="回滚检查通过",
        reason_en="rollback checks passed",
    )
    if safety is None:
        result.status = GATE_SKIP
        result.reason_zh = "未做回滚检查"
        result.reason_en = "rollback safety was not probed"
        return result
    if safety.status == SAFETY_FAIL and getattr(gates_cfg, "block_on_rollback_safety", True):
        result.status = GATE_BLOCK
        result.reason_zh = "；".join(safety.reasons_zh[:3]) or "回滚路径不可用"
        result.reason_en = "; ".join(safety.reasons_en[:3]) or "the rollback path is not available"
        result.remediation_zh = "先修复回滚条件（磁盘空间 / 状态目录 / Git 目录），再执行更新"
        result.remediation_en = "fix the rollback preconditions (disk space / state dir / git checkout) before updating"
    elif safety.status in {"WARN", "UNKNOWN"}:
        result.status = GATE_WARN
        result.reason_zh = "；".join(safety.reasons_zh[:3]) or f"回滚检查结果为 {safety.status}"
        result.reason_en = "; ".join(safety.reasons_en[:3]) or f"rollback check returned {safety.status}"
    return result


def _gate_main_branch(gates_cfg, provenance: CodeProvenance, decision: UpdateDecision) -> GateResult:
    """Main branch is a warning now: it raises Environment Risk, it does not block."""
    blocking = bool(getattr(gates_cfg, "block_main_branch_update", False))
    result = GateResult(
        key="main_branch",
        name_zh="开发分支（main）提示",
        name_en="development branch (main) notice",
        status=GATE_PASS,
        reason_zh="不在 main 开发分支",
        reason_en="not on the main development branch",
    )
    if provenance.channel == CHANNEL_MAIN:
        result.status = GATE_BLOCK if blocking else GATE_WARN
        result.caps_verdict = not blocking
        result.reason_zh = "当前安装跟踪 main 开发分支（代码可能领先正式 Release，稳定性低于正式版本）"
        result.reason_en = (
            "the installation tracks the main development branch (code may be ahead of the latest release "
            "and is less battle-tested)"
        )
        result.remediation_zh = "如果希望只跑正式版本：备份后手动 checkout 对应 tag（本工具不会自动切换）"
        result.remediation_en = (
            "to track stable only: check out a release tag after a backup (this tool never switches automatically)"
        )
    elif decision.status == UPDATE_STATUS_AHEAD:
        result.reason_zh = "代码已领先最新 Release"
        result.reason_en = "code is ahead of the latest release"
    return result


def _gate_dirty_worktree(
    gates_cfg,
    provenance: CodeProvenance,
    decision: UpdateDecision,
    overrides: Optional[OverrideReport] = None,
) -> GateResult:
    """Local changes: *known* dirty is manageable, *unknown* dirty still blocks.

    Phase 4 (doc section 14):

        dirty
        ├─ only managed, unchanged since registration   -> PASS / WARN
        ├─ managed + drifted                            -> BLOCK (configurable)
        └─ any unknown change                           -> BLOCK

    An empty registry keeps the phase-3 behaviour (any change blocks), so existing
    users see no surprise.
    """
    blocking = bool(getattr(gates_cfg, "block_dirty_worktree", True))
    block_unknown = bool(getattr(gates_cfg, "block_unknown_changes", True))
    block_drift = bool(getattr(gates_cfg, "block_drifted_overrides", True))
    result = GateResult(
        key="dirty_worktree",
        name_zh="工作区必须干净",
        name_en="clean git worktree",
        status=GATE_PASS,
        reason_zh="工作区干净",
        reason_en="worktree is clean",
    )
    if not provenance.dirty_worktree:
        return result
    if decision.status in {UPDATE_STATUS_AHEAD, UPDATE_STATUS_UP_TO_DATE}:
        result.status = GATE_WARN
        result.reason_zh = f"工作区有 {provenance.dirty_files} 个未提交修改（本次无需更新）"
        result.reason_en = f"worktree has {provenance.dirty_files} uncommitted change(s) (no update pending)"
        return result

    has_registry = bool(overrides is not None and overrides.registry and not overrides.registry.empty)
    if overrides is not None and has_registry:
        unknown = overrides.unknown_count
        drifted = overrides.drifted_count
        if unknown:
            if block_unknown:
                result.status = GATE_BLOCK
                result.reason_zh = f"存在 {unknown} 个未登记的修改（另有 {overrides.managed_count} 个已登记定制）"
                result.reason_en = (
                    f"{unknown} unregistered change(s) on top of {overrides.managed_count} managed override(s)"
                )
                result.remediation_zh = "先处理未登记的修改：登记为定制（huc overrides register）或提交/stash/删除它们"
                result.remediation_en = (
                    "deal with the unregistered changes first: register them (`huc overrides register`), "
                    "commit them, stash them, or remove them"
                )
            else:
                result.status = GATE_WARN
                result.reason_zh = f"存在 {unknown} 个未登记的修改"
                result.reason_en = f"{unknown} unregistered change(s)"
            return result
        if drifted:
            if block_drift:
                result.status = GATE_BLOCK
                result.reason_zh = f"{drifted} 个本地定制在登记之后又被修改过（drift）：先 refresh 或确认"
                result.reason_en = f"{drifted} override(s) drifted since registration: refresh or confirm them first"
                result.remediation_zh = "确认改动后运行 huc overrides refresh（把当前内容作为新基线）"
                result.remediation_en = "run `huc overrides refresh` after reviewing the change (new baseline)"
            else:
                result.status = GATE_WARN
                result.reason_zh = f"{drifted} 个本地定制有 drift"
                result.reason_en = f"{drifted} override(s) drifted"
            return result
        # only managed, unchanged changes
        result.status = GATE_PASS
        result.reason_zh = f"工作区有 {overrides.managed_count} 个本地定制，全部与登记时一致；未登记修改 0 个"
        result.reason_en = (
            f"{overrides.managed_count} managed local override(s), all unchanged since registration; "
            "0 unregistered changes"
        )
        return result

    if blocking:
        result.status = GATE_BLOCK
        result.reason_zh = f"工作区有 {provenance.dirty_files} 个未提交修改"
        result.reason_en = f"worktree has {provenance.dirty_files} uncommitted change(s)"
        result.remediation_zh = "先提交或 stash 本地修改；如果是你有意保留的定制，用 `huc overrides register` 登记它们"
        result.remediation_en = (
            "commit or stash local changes first; if they are intentional customizations, "
            "register them with `huc overrides register`"
        )
    else:
        result.status = GATE_WARN
        result.reason_zh = f"工作区有 {provenance.dirty_files} 个未提交修改"
        result.reason_en = f"worktree has {provenance.dirty_files} uncommitted change(s)"
    return result


def _gate_prerelease(gates_cfg, provenance: CodeProvenance, decision: UpdateDecision) -> GateResult:
    """Prerelease is a warning by default: the user asked for it by installing one."""
    blocking = bool(getattr(gates_cfg, "block_prerelease", False))
    result = GateResult(
        key="prerelease",
        name_zh="预发布版本提示",
        name_en="prerelease notice",
        status=GATE_PASS,
        reason_zh="非预发布版本",
        reason_en="not a prerelease",
    )
    if provenance.channel != CHANNEL_PRERELEASE:
        return result
    result.status = GATE_BLOCK if blocking else GATE_WARN
    result.caps_verdict = not blocking
    result.reason_zh = "当前运行的是预发布版本（prerelease/beta/rc）"
    result.reason_en = "currently running a prerelease build (prerelease/beta/rc)"
    result.remediation_zh = "确认知悉风险后再更新；或切回正式 Release"
    result.remediation_en = "accept the risk explicitly before updating, or move back to a stable release"
    return result


def _gate_release_age(
    cfg: Config,
    release: Optional[Release],
    decision: UpdateDecision,
    now: datetime,
    report: GateReport,
) -> GateResult:
    """Segmented policy (doc section 9): only a very fresh release blocks."""
    policy: ReleaseAgePolicy = getattr(cfg, "release_age_policy", None) or ReleaseAgePolicy()
    result = GateResult(
        key="release_age",
        name_zh=f"Release 年龄（<{policy.block_hours:g}h 阻断 / <{policy.caution_hours:g}h 谨慎）",
        name_en=f"release age (<{policy.block_hours:g}h blocked / <{policy.caution_hours:g}h caution)",
        status=GATE_PASS,
        reason_zh="发布已过谨慎观察期",
        reason_en="release is past the caution window",
    )
    if decision.status in {UPDATE_STATUS_AHEAD, UPDATE_STATUS_UP_TO_DATE}:
        result.status = GATE_SKIP
        result.reason_zh = "无需更新，不适用"
        result.reason_en = "no update to evaluate"
        return result
    if release is None or release.when is None:
        result.status = GATE_WARN
        result.reason_zh = "无法确定发布时间，无法确认发布年龄"
        result.reason_en = "release date unknown; the release age cannot be verified"
        return result

    age_hours = release.age_hours if release.age_hours is not None else 0.0
    band = policy.band(age_hours)
    report.age_band = band
    if release.when is not None:
        report.policy_clearance = release.when + timedelta(hours=policy.block_hours)
        report.caution_clearance = release.when + timedelta(hours=policy.caution_hours)

    if band == "block":
        result.status = GATE_BLOCK
        result.reason_zh = f"Release 仅发布 {age_hours:.1f} 小时（< {policy.block_hours:g}h）：太新，先让它跑一会儿"
        result.reason_en = (
            f"release is only {age_hours:.1f} hours old (< {policy.block_hours:g}h): too fresh, let it settle"
        )
        result.earliest_recheck = report.policy_clearance
        result.remediation_zh = "等到最早可放行时间（Earliest Policy Clearance）后再评估"
        result.remediation_en = "re-evaluate after the earliest policy clearance time"
    elif band == "caution":
        result.status = GATE_WARN
        result.reason_zh = f"Release 发布 {age_hours:.1f} 小时（{policy.block_hours:g}-{policy.caution_hours:g}h）：谨慎区间，最多给出 ACCEPTABLE"
        result.reason_en = (
            f"release is {age_hours:.1f} hours old ({policy.block_hours:g}-{policy.caution_hours:g}h): "
            "caution band, the verdict is capped at ACCEPTABLE"
        )
    elif band == "acceptable":
        result.reason_zh = f"Release 发布 {age_hours:.1f} 小时：已经过谨慎区间"
        result.reason_en = f"release is {age_hours:.1f} hours old: past the caution window"
    return result


def _gate_regression_warning(
    enabled: bool,
    cluster_key: str,
    label_zh: str,
    label_en: str,
    clusters: Sequence[RegressionCluster],
) -> GateResult:
    """Ordinary regression classes warn and cap the verdict - they never block."""
    result = GateResult(
        key=f"warn_{cluster_key.lower()}_regression",
        name_zh=f"{label_zh}提示",
        name_en=f"{label_en} notice",
        status=GATE_PASS if enabled else GATE_SKIP,
        reason_zh="无活跃回归报告",
        reason_en="no active regression reports",
    )
    if not enabled:
        return result
    active = [
        cluster
        for cluster in clusters
        if cluster.key == cluster_key
        and cluster.open_count > 0
        and cluster.confidence in {CONFIDENCE_HIGH, CONFIDENCE_MEDIUM}
    ]
    if not active:
        return result
    worst = active[0]
    result.status = GATE_WARN
    result.reason_zh = (
        f"{worst.zh} 存在 {worst.reports} 个报告（{worst.unique_reporters} 位报告人，"
        f"{worst.open_count} 个 open），严重程度 {worst.severity}，可信度 {worst.confidence}"
    )
    result.reason_en = (
        f"{worst.reports} report(s) of {worst.en} ({worst.unique_reporters} reporter(s), "
        f"{worst.open_count} open), severity {worst.severity}, confidence {worst.confidence}"
    )
    features = ", ".join(worst.affected_features[:4])
    if features:
        result.detail_zh = f"影响范围：{features}"
        result.detail_en = f"affects: {features}"
    return result


def describe_gate_lines(report: GateReport, *, lang: str = "zh") -> list[str]:
    """Compact one-line-per-gate rendering used by the report and the CLI."""
    lines: list[str] = []
    for gate in report.gates:
        if gate.status == GATE_SKIP:
            continue
        label = gate.name_zh if lang == "zh" else gate.name_en
        reason = gate.reason_zh if lang == "zh" else gate.reason_en
        lines.append(f"{gate.status:<5} {label}" + (f" — {reason}" if reason and gate.status != GATE_PASS else ""))
    return lines


def severe_cluster_keys(clusters, *, threshold: str = SEVERITY_HIGH) -> list[str]:
    from .clusters import SEVERITY_RANK

    minimum = SEVERITY_RANK.get(threshold, 2)
    return [c.key for c in clusters if SEVERITY_RANK.get(c.severity, 0) >= minimum]


def critical_cluster_keys(clusters) -> list[str]:
    return [c.key for c in clusters if c.severity == SEVERITY_CRITICAL]
