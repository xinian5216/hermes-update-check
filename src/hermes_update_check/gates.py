"""Hard gates: rules that override the risk score.

The score answers "how risky does this look"; gates answer "is updating allowed
at all right now". A dirty worktree or a 14-hour-old release blocks an update
even when the computed risk is low - that is the whole point.

Priority order (implemented in :mod:`hermes_update_check.advisor`):

1. insufficient data
2. abnormal local environment
3. **hard gates**  (this module)
4. risk score
5. release cooling period
6. update
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .clusters import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    RegressionCluster,
)
from .config import Config
from .local_env import LocalEnv
from .logging_setup import get_logger
from .provenance import (
    CHANNEL_MAIN,
    CHANNEL_PRERELEASE,
    CodeProvenance,
    UpdateDecision,
    UPDATE_STATUS_AHEAD,
    channel_mismatch,
)
from .risk import RiskAssessment
from .util import iso, utcnow
from .github_api import Release

GATE_BLOCK = "BLOCK"
GATE_WARN = "WARN"
GATE_PASS = "PASS"
GATE_SKIP = "SKIP"

#: Cluster key -> (config attribute, gate key, zh label, en label)
REGRESSION_GATES: tuple[tuple[str, str, str, str], ...] = (
    ("DATABASE", "block_active_database_regression", "数据库回归", "active database regression"),
    ("SESSION", "block_active_session_regression", "Session/数据丢失回归", "active session/data-loss regression"),
    ("GATEWAY", "block_active_gateway_regression", "Gateway 回归", "active gateway regression"),
    ("UPDATE_FAILURE", "block_active_update_failure", "升级失败回归", "active update-failure regression"),
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
        }


@dataclass
class GateReport:
    enabled: bool = True
    gates: list[GateResult] = field(default_factory=list)
    environment: Optional[EnvironmentState] = None
    channel_warning: Optional[tuple[str, str]] = None

    @property
    def blocking(self) -> list[GateResult]:
        return [g for g in self.gates if g.status == GATE_BLOCK]

    @property
    def warnings(self) -> list[GateResult]:
        return [g for g in self.gates if g.status == GATE_WARN]

    @property
    def blocked(self) -> bool:
        return bool(self.blocking)

    @property
    def environment_abnormal(self) -> bool:
        return bool(self.environment and self.environment.abnormal)

    def blocked_by(self, key: str) -> bool:
        return any(g.key == key and g.blocking for g in self.gates)

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
    clusters: list[RegressionCluster] = (),  # type: ignore[assignment]
    environment: Optional[EnvironmentState] = None,
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
    report.gates.append(_gate_release_age(gates_cfg, release, decision, moment))
    report.gates.append(_gate_main_branch(gates_cfg, provenance, decision))
    report.gates.append(_gate_dirty_worktree(gates_cfg, provenance, decision))
    report.gates.append(_gate_prerelease(gates_cfg, provenance, decision))
    for cluster_key, attr, label_zh, label_en in REGRESSION_GATES:
        report.gates.append(
            _gate_regression(getattr(gates_cfg, attr, False), cluster_key, label_zh, label_en, clusters)
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
# individual gates
# --------------------------------------------------------------------------- #


def _gate_insufficient_data(
    cfg: Config,
    gates_cfg,  # noqa: ANN001
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
    if decision.status in {UPDATE_STATUS_AHEAD, "up_to_date"}:
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


def _gate_release_age(
    gates_cfg,  # noqa: ANN001
    release: Optional[Release],
    decision: UpdateDecision,
    now: datetime,
) -> GateResult:
    minimum_hours = float(getattr(gates_cfg, "minimum_release_age_hours", 48.0))
    result = GateResult(
        key="release_age",
        name_zh=f"Release 年龄 >= {minimum_hours:g}h",
        name_en=f"release at least {minimum_hours:g}h old",
        status=GATE_PASS,
        reason_zh=f"发布已满 {minimum_hours:g} 小时",
        reason_en=f"release is older than {minimum_hours:g} h",
    )
    if minimum_hours <= 0:
        result.status = GATE_SKIP
        return result
    if decision.status in {UPDATE_STATUS_AHEAD, "up_to_date"}:
        result.status = GATE_SKIP
        result.reason_zh = "无需更新，不适用"
        result.reason_en = "no update to evaluate"
        return result
    if release is None or release.when is None:
        result.status = GATE_BLOCK
        result.reason_zh = "无法确定发布时间，无法确认观察期"
        result.reason_en = "release date unknown; the observation window cannot be verified"
        return result

    age_hours = release.age_hours if release.age_hours is not None else 0.0
    if age_hours < minimum_hours:
        deadline = release.when + timedelta(hours=minimum_hours)
        result.status = GATE_BLOCK
        result.reason_zh = (
            f"Release 仅发布 {age_hours:.1f} 小时，低于最小观察期 {minimum_hours:g} 小时"
        )
        result.reason_en = (
            f"release is only {age_hours:.1f} hours old; the minimum observation period is {minimum_hours:g} h"
        )
        result.earliest_recheck = deadline
        result.remediation_zh = "等观察期结束、且没有新的严重 Issue 后再评估"
        result.remediation_en = "wait for the observation window and no new severe issues before re-evaluating"
    return result


def _gate_main_branch(gates_cfg, provenance: CodeProvenance, decision: UpdateDecision) -> GateResult:  # noqa: ANN001
    blocking = bool(getattr(gates_cfg, "block_main_branch_update", True))
    on_main = provenance.channel == CHANNEL_MAIN
    result = GateResult(
        key="main_branch",
        name_zh="禁止在 main 开发分支上执行更新",
        name_en="no update while tracking the main development branch",
        status=GATE_PASS,
        reason_zh="不在 main 开发分支",
        reason_en="not on the main development branch",
    )
    if provenance.channel == CHANNEL_MAIN:
        if blocking:
            result.status = GATE_BLOCK
            result.reason_zh = "当前安装跟踪 main 开发分支（代码可能领先正式 Release）"
            result.reason_en = "the installation tracks the main development branch (code may be ahead of the latest release)"
        else:
            result.status = GATE_WARN
            result.reason_zh = "当前安装跟踪 main 开发分支"
            result.reason_en = "the installation tracks the main development branch"
        result.remediation_zh = "如需更新，先切回正式 Release（备份后执行 git checkout <tag>）；本工具不会自动切换"
        result.remediation_en = "to update, switch back to a release tag first (git checkout <tag> after a backup); this tool never switches automatically"
    elif decision.status == UPDATE_STATUS_AHEAD:
        result.reason_zh = "代码已领先最新 Release"
        result.reason_en = "code is ahead of the latest release"
    return result


def _gate_dirty_worktree(gates_cfg, provenance: CodeProvenance, decision: UpdateDecision) -> GateResult:  # noqa: ANN001
    blocking = bool(getattr(gates_cfg, "block_dirty_worktree", True))
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
    if decision.status in {UPDATE_STATUS_AHEAD, "up_to_date"}:
        result.status = GATE_WARN
        result.reason_zh = f"工作区有 {provenance.dirty_files} 个未提交修改（本次无需更新）"
        result.reason_en = f"worktree has {provenance.dirty_files} uncommitted change(s) (no update pending)"
        return result
    if blocking:
        result.status = GATE_BLOCK
        result.reason_zh = f"工作区有 {provenance.dirty_files} 个未提交修改"
        result.reason_en = f"worktree has {provenance.dirty_files} uncommitted change(s)"
        result.remediation_zh = "先提交或 stash 本地修改（更新会 stash，但可能与新版冲突）"
        result.remediation_en = "commit or stash local changes first (the update stashes them, but conflicts are possible)"
    else:
        result.status = GATE_WARN
        result.reason_zh = f"工作区有 {provenance.dirty_files} 个未提交修改"
        result.reason_en = f"worktree has {provenance.dirty_files} uncommitted change(s)"
    return result


def _gate_prerelease(gates_cfg, provenance: CodeProvenance, decision: UpdateDecision) -> GateResult:  # noqa: ANN001
    blocking = bool(getattr(gates_cfg, "block_prerelease", True))
    result = GateResult(
        key="prerelease",
        name_zh="禁止预发布版本",
        name_en="no prerelease builds",
        status=GATE_PASS,
        reason_zh="非预发布版本",
        reason_en="not a prerelease",
    )
    if provenance.channel != CHANNEL_PRERELEASE:
        return result
    result.status = GATE_BLOCK if blocking else GATE_WARN
    result.reason_zh = "当前运行的是预发布版本（prerelease/beta/rc）"
    result.reason_en = "currently running a prerelease build (prerelease/beta/rc)"
    result.remediation_zh = "确认知悉风险后再更新；或切回正式 Release"
    result.remediation_en = "accept the risk explicitly before updating, or move back to a stable release"
    return result


def _gate_regression(
    enabled: bool,
    cluster_key: str,
    label_zh: str,
    label_en: str,
    clusters,  # noqa: ANN001
) -> GateResult:
    result = GateResult(
        key=f"active_{cluster_key.lower()}_regression",
        name_zh=f"禁止存在活跃的{label_zh}",
        name_en=f"no active {label_en}",
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
    result.status = GATE_BLOCK
    result.reason_zh = (
        f"{worst.zh} 存在 {worst.reports} 个报告（{worst.unique_reporters} 位报告人，"
        f"{worst.open_count} 个 open），严重程度 {worst.severity}，可信度 {worst.confidence}"
    )
    result.reason_en = (
        f"{worst.reports} report(s) of {worst.en} ({worst.unique_reporters} reporter(s), "
        f"{worst.open_count} open), severity {worst.severity}, confidence {worst.confidence}"
    )
    result.remediation_zh = "等这些 Issue 关闭或出现修复版本后再更新"
    result.remediation_en = "wait until those issues are closed or a fix release ships"
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


def severe_cluster_keys(clusters, *, threshold: str = SEVERITY_HIGH) -> list[str]:  # noqa: ANN001
    from .clusters import SEVERITY_RANK

    minimum = SEVERITY_RANK.get(threshold, 2)
    return [c.key for c in clusters if SEVERITY_RANK.get(c.severity, 0) >= minimum]


def critical_cluster_keys(clusters) -> list[str]:  # noqa: ANN001
    return [c.key for c in clusters if c.severity == SEVERITY_CRITICAL]
