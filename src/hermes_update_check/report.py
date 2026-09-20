"""Report rendering: the human-readable answer, in Chinese or English.

Layout (phase 3): the global risk is background, the personal reading decides.

    LOCAL INSTALLATION   - code provenance: channel, branch, commit, ahead/behind
    LATEST STABLE        - the release on GitHub
    UPDATE STATUS        - is an update even the right question?
    GLOBAL RISK          - Change Risk / Regression Signal / Data Confidence / Overall
    PERSONAL READINESS   - Personal Impact / Core Feature Readiness / Rollback Safety
    YOUR CORE FEATURES   - one row per feature of your usage profile
    KNOWN ISSUES         - each cluster with severity/confidence *and* whether you use it
    SYSTEMIC RISKS       - the categories that are never negotiable
    GATES & CAUTIONS     - what blocks, what merely warns
    RECOMMENDATION       - the action, why, and the two different clocks

Machine output stays free of ANSI/Rich markup: `--json` comes from
``UpdateCheck.to_dict()``, and the markdown writer emits plain text only.
"""

from __future__ import annotations

from typing import Optional

from .advisor import (
    RECOMMEND_ACCEPTABLE,
    RECOMMEND_BLOCKED,
    RECOMMEND_SAFE,
    RECOMMEND_WAIT,
)
from .checker import UpdateCheck, _working_tree_label
from .clusters import RegressionCluster
from .console import Console
from .gates import GATE_PASS, GATE_SKIP, GateReport
from .overrides import OverrideReport
from .provenance import UPDATE_STATUS_AHEAD
from .risk import (
    RECOMMEND_UNKNOWN,
    RiskAssessment,
)
from .rollback_safety import SAFETY_FAIL, SAFETY_PASS, SAFETY_UNKNOWN, SAFETY_WARN
from .util import humanize_hours, iso

REPORT_TITLE = "Hermes Update Advisor"

SECTION_LOCAL = ("本地安装状态", "LOCAL INSTALLATION")
SECTION_RELEASE = ("最新正式版本", "LATEST STABLE")
SECTION_STATUS = ("更新状态", "UPDATE STATUS")
SECTION_RISK = ("全局风险（背景信息）", "GLOBAL RISK (background)")
SECTION_PERSONAL = ("个人就绪度", "PERSONAL READINESS")
SECTION_FEATURES = ("你的核心功能", "YOUR CORE FEATURES")
SECTION_ISSUES = ("已知问题", "KNOWN ISSUES")
SECTION_SYSTEMIC = ("系统级风险", "SYSTEMIC RISKS")
SECTION_OVERRIDES = ("本地定制", "LOCAL CUSTOMIZATION")
SECTION_GATES = ("门禁与提示", "GATES & CAUTIONS")
SECTION_REGRESSIONS = ("回归信号", "REGRESSION SIGNALS")
SECTION_FACTORS = ("风险因子明细", "RISK FACTOR BREAKDOWN")
SECTION_RECOMMENDATION = ("建议", "RECOMMENDATION")
SECTION_ENV = ("本地环境", "LOCAL ENVIRONMENT")
SECTION_NOTES = ("备注", "NOTES")


def _format_age(seconds: float, *, zh: bool) -> str:
    """Compact human age for cache-staleness labels (minutes dominate below 2h)."""
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} 分钟" if zh else f"{minutes} min"
    hours = minutes / 60
    return f"{hours:.1f} 小时" if zh else f"{hours:.1f} h"


class Reporter:
    """Renders an UpdateCheck for humans (rich) or machines (markdown/json)."""

    def __init__(self, console: Console, *, lang: str = "zh") -> None:
        self.console = console
        self.lang = lang

    # -- helpers ------------------------------------------------------------- #

    def _t(self, zh: str, en: str) -> str:
        return zh if self.lang == "zh" else en

    def _section(self, pair: tuple[str, str]) -> str:
        return pair[0] if self.lang == "zh" else pair[1]

    # -- public API ---------------------------------------------------------- #

    def render(self, check: UpdateCheck, *, detailed: bool = False) -> None:
        console = self.console
        console.blank()
        console.print_markup(f"[bold]{REPORT_TITLE}[/bold]")
        self._render_data_freshness(check)
        console.blank()

        self._render_provenance(check)
        self._render_release(check)
        self._render_update_status(check)
        if check.assessment is not None:
            self._render_risk(check.assessment, check)
        self._render_personal(check.readiness, check.rollback_safety)
        self._render_features(check.readiness)
        self._render_known_issues(check.readiness)
        self._render_systemic(check.readiness)
        self._render_overrides(check.overrides)
        self._render_gates(check.gates)
        self._render_regressions(check.clusters, detailed=detailed)
        if check.assessment is not None:
            self._render_factors(check.assessment, detailed=detailed)
        self._render_recommendation(check)
        if detailed:
            self._render_issue_samples(check)
            self._render_local_block(check)
        self._render_notes(check)

    # -- sections ------------------------------------------------------------ #

    def _render_data_freshness(self, check: UpdateCheck) -> None:
        """Say plainly whether the release data behind this report is live or cached."""
        if not check.releases:
            return
        if check.release_data_from_cache:
            age = check.release_data_age_seconds or 0.0
            label = _format_age(age, zh=self.lang == "zh")
            msg = (
                f"更新数据：缓存（约 {label}前获取，GitHub 不可达时的兜底）"
                if self.lang == "zh"
                else f"Release data: cached (fetched ~{label} ago; offline fallback)"
            )
        else:
            msg = (
                "更新数据：实时（刚从 GitHub 获取）"
                if self.lang == "zh"
                else "Release data: live (just fetched from GitHub)"
            )
        self.console.print_markup(f"[dim]{msg}[/dim]")

    def _render_provenance(self, check: UpdateCheck) -> None:
        prov = check.provenance
        console = self.console
        console.heading(self._section(SECTION_LOCAL))
        if prov is None:
            console.kv_table([("Current Version", check.env.version_label)])
            return
        rows: list[tuple[str, str]] = [
            ("Reported Version", f"v{prov.reported_version}" if prov.reported_version else "unknown"),
            (
                "Channel",
                f"{prov.channel}   ({prov.channel_note_zh() if self.lang == 'zh' else prov.channel_note_en()})",
            ),
        ]
        if prov.git_branch:
            rows.append(("Git Branch", prov.git_branch))
        if prov.git_commit:
            rows.append(("Git Commit", prov.git_commit))
        if prov.nearest_tag:
            rows.append(
                (
                    "Nearest Release",
                    prov.nearest_tag + (f" (v{prov.nearest_tag_version})" if prov.nearest_tag_version else ""),
                )
            )
        if prov.commits_ahead_of_tag is not None:
            rows.append(("Ahead of Release", f"{prov.commits_ahead_of_tag} commits"))
        if prov.commits_behind_target is not None:
            rows.append(("Behind Latest", f"{prov.commits_behind_target} commits"))
        if prov.is_git_install:
            rows.append(("Working Tree", _working_tree_label(prov, lang=self.lang)))
        console.kv_table(rows)
        console.blank()

    def _render_release(self, check: UpdateCheck) -> None:
        console = self.console
        console.heading(self._section(SECTION_RELEASE))
        if check.latest is None:
            console.print(
                self._t(
                    "拿不到 Release 信息（GitHub 不可达且无可用缓存）",
                    "no release information (GitHub unreachable, no usable cache)",
                )
            )
            console.blank()
            return
        latest = check.latest
        rows: list[tuple[str, str]] = [
            ("Release", f"v{latest.display_version} ({latest.tag})" if latest.display_version else latest.tag),
        ]
        if latest.age_hours is not None:
            rows.append(
                (
                    "Published",
                    f"{humanize_hours(latest.age_hours, lang=self.lang)}"
                    + self._t("前", " ago")
                    + (f"  ({iso(latest.when)})" if latest.when else ""),
                )
            )
        if check.compare is not None:
            rows.append(("Commits", f"{check.compare.total_commits}" + ("+" if check.compare.truncated else "")))
        if check.pr_count is not None:
            rows.append(
                (
                    "Merged PRs",
                    f"{check.pr_count}"
                    + (self._t("（来自 Release Notes）", " (from release notes)") if check.pr_count_from_notes else ""),
                )
            )
        if latest.prerelease:
            rows.append(("Prerelease", "yes"))
        if latest.html_url:
            rows.append(("Release page", latest.html_url))
        console.kv_table(rows)
        console.blank()
        if self.lang == "en":
            console.print("Latest != safest: the newest tag is only worth taking once it has matured.")
        else:
            console.print("最新版本不等于最安全版本：新版需要先经过观察期。")
        console.blank()

    def _render_update_status(self, check: UpdateCheck) -> None:
        decision = check.decision
        console = self.console
        console.heading(self._section(SECTION_STATUS))
        if decision is None:
            console.print(check.env.version_label)
            console.blank()
            return
        headline = decision.headline_zh if self.lang == "zh" else decision.headline_en
        console.print(f"  {headline}")
        if decision.message_zh or decision.message_en:
            message = decision.message_zh if self.lang == "zh" else decision.message_en
            if message:
                for line in _wrap(message):
                    console.print(f"  {line}")
        if decision.status == UPDATE_STATUS_AHEAD and check.provenance is not None:
            console.blank()
            console.print(
                self._t(
                    "  → 开发分支模式：请勿把“切到 v%s”当作升级；它可能是一次降级。" % (decision.target_version or "?"),
                    "  -> development channel mode: moving to %s is not an upgrade, it may be a downgrade."
                    % (decision.target_version or "?"),
                )
            )
        console.blank()

    def _render_risk(self, assessment: RiskAssessment, check: UpdateCheck) -> None:
        console = self.console
        console.heading(self._section(SECTION_RISK))
        rows = assessment.component_rows()
        console.kv_table(rows)
        if assessment.score_is_lower_bound:
            console.blank()
            console.warn(
                self._t(
                    "数据不完整：Overall Risk 是下界（known risk >= N），缺失证据不会被当作零风险。",
                    "incomplete data: Overall Risk is a lower bound (known risk >= N); missing evidence is not zero risk.",
                )
            )
        if check.issues is not None and check.issues.available and check.issues.signal_confidence is not None:
            console.print(
                self._t(
                    f"  Issue Signal Confidence: {check.issues.signal_confidence}/100"
                    f"（扫描 {check.issues.scanned_items} 条，聚类 {len(check.clusters)} 个）",
                    f"  Issue Signal Confidence: {check.issues.signal_confidence}/100 "
                    f"(scanned {check.issues.scanned_items}, clusters {len(check.clusters)})",
                )
            )
        console.blank()

    def _render_overrides(self, report: Optional[OverrideReport]) -> None:
        """Managed local overrides: only shown when there is something to show."""
        if report is None:
            return
        has_registry = bool(report.registry and not report.registry.empty)
        # no registry and a clean tree: one line would be noise, the gate says it
        if not has_registry and not report.managed_count and not report.unknown_count:
            return
        console = self.console
        console.heading(self._section(SECTION_OVERRIDES))
        rows = [
            ("Managed Overrides", str(report.managed_count)),
            ("Unknown Changes", str(report.unknown_count)),
            ("Drifted Overrides", str(report.drifted_count)),
        ]
        if report.missing_count:
            rows.append(("Missing Overrides", str(report.missing_count)))
        rows.append(("Override Safety", report.safety))
        if report.prediction is not None and report.prediction.confidence:
            target = report.prediction.target or "-"
            rows.append(
                (
                    self._t("重新应用把握 Reapply Confidence", "Reapply Confidence"),
                    f"{report.prediction.confidence} ({target})",
                )
            )
        console.kv_table(rows)
        # File rows go through console.print: a long path in a rich table squeezes
        # the value column to nothing (the status disappeared when redirected).
        for entry in report.registry.files[:12] if report.registry else []:
            path = entry.path if len(entry.path) <= 74 else "..." + entry.path[-71:]
            console.print(f"  {entry.match.upper():8} {path}")
        if has_registry:
            for line in report.reasons_zh if self.lang == "zh" else report.reasons_en:
                console.print(f"  · {line}")
            if report.prediction is not None and report.prediction.manual_merge_likely:
                console.print(self._t("  ！很可能需要人工合并（LOW）", "  ! manual merge likely required (LOW)"))
        console.blank()

    def _render_gates(self, gates: Optional[GateReport]) -> None:
        console = self.console
        console.heading(self._section(SECTION_GATES))
        if gates is None:
            console.print(self._t("未评估", "not evaluated"))
            console.blank()
            return
        if not gates.enabled:
            console.print(
                self._t(
                    "硬门禁已禁用（hard_gates.enabled = false）", "hard gates disabled (hard_gates.enabled = false)"
                )
            )
            console.blank()
            return
        visible = [g for g in gates.gates if g.status != GATE_SKIP]
        if not visible:
            console.print(self._t("无适用门禁", "no applicable gates"))
        for gate in visible:
            label = gate.name_zh if self.lang == "zh" else gate.name_en
            reason = gate.reason_zh if self.lang == "zh" else gate.reason_en
            console.print(f"  {gate.status:<5} {label}")
            if gate.status != GATE_PASS and reason:
                console.print(f"        {reason}")
        if gates.blocked:
            console.blank()
            console.print(
                self._t(
                    "阻断类门禁（并只保留系统级风险、关键工作流、回滚路径、本地工作区、数据不足）优先于任何分数；"
                    "提示类门禁只把结论限制在 ACCEPTABLE，不再阻止更新。",
                    "blocking gates (systemic risk, critical workflow, rollback path, local worktree, "
                    "insufficient data) outrank any score; the warning gates only cap the verdict at "
                    "ACCEPTABLE instead of stopping the update.",
                )
            )
        console.blank()

    def _render_regressions(self, clusters: list[RegressionCluster], *, detailed: bool) -> None:
        console = self.console
        console.heading(self._section(SECTION_REGRESSIONS))
        if not clusters:
            console.print(self._t("未发现达标的回归聚类", "no qualifying regression clusters"))
            console.blank()
            return
        width = max(len(c.en if self.lang == "en" else c.zh) for c in clusters)
        for cluster in clusters:
            label = cluster.en if self.lang == "en" else cluster.zh
            console.print(f"  {label.ljust(width)}  {cluster.severity:<8} {cluster.confidence}")
            console.print(
                "      "
                + (
                    self._t(
                        f"{cluster.reports} 个报告 / {cluster.unique_reporters} 位独立报告人 / "
                        f"{cluster.open_count} 个 open / maintainer 确认 {cluster.maintainer_confirmed} / "
                        f"linked PR {cluster.linked_pr}",
                        f"{cluster.reports} reports / {cluster.unique_reporters} unique reporters / "
                        f"{cluster.open_count} open / {cluster.maintainer_confirmed} maintainer-confirmed / "
                        f"linked PR {cluster.linked_pr}",
                    )
                )
            )
            for note in cluster.notes_zh if self.lang == "zh" else cluster.notes_en:
                console.print(f"      - {note}")
            if detailed:
                for issue in cluster.samples[:2]:
                    console.print(f"      #{issue.number} [{issue.state}] {issue.title[:96]}")
                    if issue.html_url:
                        console.print(f"        {issue.html_url}")
        console.blank()

    def _render_factors(self, assessment: RiskAssessment, *, detailed: bool) -> None:
        console = self.console
        factors = [f for f in assessment.factors if detailed or abs(f.points) >= 0.5]
        if not factors:
            return
        console.heading(self._section(SECTION_FACTORS))
        rows = []
        for factor in factors:
            label = factor.label_zh if self.lang == "zh" else factor.label_en
            detail = factor.detail_zh if self.lang == "zh" else factor.detail_en
            points = f"{factor.points:+.1f}"
            if factor.unknown:
                points += " (UNKNOWN)"
            rows.append((f"{label}  {points}", detail))
        console.kv_table(rows)
        console.blank()

    def _render_personal(self, readiness, safety) -> None:
        """Personal Impact / Core Feature Readiness / Rollback Safety (phase 3)."""
        console = self.console
        console.heading(self._section(SECTION_PERSONAL))
        if readiness is None:
            console.print(self._t("未计算个人就绪度。", "personal readiness was not computed."))
            console.blank()
            return
        rows = [
            (
                self._t("个人影响 Personal Impact", "Personal Impact"),
                f"{readiness.impact if readiness.impact is not None else 'UNKNOWN'} / 100  {readiness.impact_level}",
            ),
            (
                self._t("核心功能可用性 Core Readiness", "Core Feature Readiness"),
                f"{readiness.readiness if readiness.readiness is not None else 'UNKNOWN'} "
                f"/ 100  {readiness.readiness_level}",
            ),
        ]
        if safety is not None:
            label = {
                SAFETY_PASS: self._t("通过", "PASS"),
                SAFETY_WARN: self._t("警告", "WARN"),
                SAFETY_FAIL: self._t("失败（会阻断更新）", "FAIL (blocks the update)"),
                SAFETY_UNKNOWN: self._t("未知（按不安全处理）", "UNKNOWN (treated as unsafe)"),
            }.get(safety.status, safety.status)
            rows.append((self._t("回滚路径 Rollback Safety", "Rollback Safety"), label))
        source = {
            "config": self._t("来自配置 usage_profile", "from usage_profile in your config"),
            "detected": self._t(
                "自动检测（运行 `profile detect` 后请人工调整）", "auto-detected (run `profile detect` and review)"
            ),
            "builtin-default": self._t("内置默认画像", "built-in default profile"),
        }.get(readiness.profile.source, readiness.profile.source)
        rows.append((self._t("画像来源 Profile", "Profile source"), source))
        console.kv_table(rows)
        console.blank()
        console.print(
            self._t(
                "  个人影响 = 已知问题对你所用功能的暴露程度；核心可用性 = 关键工作流是否真的不可用。",
                "  Personal impact = how much known issues touch what you use; readiness = whether a "
                "workflow is actually unavailable.",
            )
        )
        console.blank()

    def _render_features(self, readiness) -> None:
        console = self.console
        if readiness is None:
            return
        console.heading(self._section(SECTION_FEATURES))
        featured = [f for f in readiness.features if f.cluster_keys or not f.is_unused]
        if not featured:
            console.print(self._t("没有命中任何已知回归类别。", "no known regression class matches your features."))
            console.blank()
            return
        rows = []
        for feature in featured[:14]:
            label = readiness.profile.label(feature.key, lang=self.lang)
            detail = f"{feature.status}"
            if feature.is_unused:
                detail += self._t("（未使用，不计入）", " (unused, not counted)")
            elif feature.cluster_keys:
                detail += f"  {feature.severity or '-'} / {feature.confidence or '-'}"
                if feature.unavailable:
                    detail += self._t(" · 报告称不可用", " · reported unavailable")
            rows.append((label, detail))
        console.kv_table(rows)
        console.blank()

    def _render_known_issues(self, readiness) -> None:
        """Every cluster, with the profile verdict next to it (doc section 23)."""
        console = self.console
        if readiness is None or not readiness.features:
            return
        console.heading(self._section(SECTION_ISSUES))
        rows = []
        for feature in readiness.features:
            if not feature.cluster_keys:
                continue
            label = readiness.profile.label(feature.key, lang=self.lang)
            level = feature.level
            rows.append(
                (
                    f"{label}",
                    f"{feature.severity:<8} {feature.confidence:<8} {level.upper()}",
                )
            )
        for row in rows[:12]:
            console.kv_table([row])
        console.blank()

    def _render_systemic(self, readiness) -> None:
        console = self.console
        console.heading(self._section(SECTION_SYSTEMIC))
        if readiness is None:
            console.print(self._t("未计算。", "not computed."))
            console.blank()
            return
        rows = []
        for risk in readiness.systemic:
            label = risk.zh if self.lang == "zh" else risk.en
            if risk.detected:
                state = self._t("发现证据", "detected")
                if risk.source == "local":
                    state = self._t("本地检查未通过（阻断）", "local check failed (blocks)")
                elif risk.blocking:
                    state = self._t("可信度高（阻断）", "high confidence (blocks)")
                else:
                    state = self._t("证据不足（仅提示）", "not corroborated (watch only)")
            else:
                state = self._t("无", "none")
            rows.append((label, state))
        console.kv_table(rows)
        console.blank()

    def _render_recommendation(self, check: UpdateCheck) -> None:
        console = self.console
        console.heading(self._section(SECTION_RECOMMENDATION))
        rec = check.recommendation
        if rec is None:
            for line in self.recommendation_lines(check, check.assessment):
                console.print(line)
            console.blank()
            return

        console.print_markup(f"[bold]{rec.action}[/bold]")
        console.blank()
        headline = rec.headline_zh if self.lang == "zh" else rec.headline_en
        if headline:
            console.print(f"  {headline}")
        explanation = rec.explanation_zh if self.lang == "zh" else rec.explanation_en
        if explanation:
            for line in _wrap(explanation, width=86):
                console.print("  " + line)
        console.print(
            self._t(
                f"  （由 {rec.decided_by_label} 决定；"
                f"你的风险：个人影响 {rec.personal_impact if rec.personal_impact is not None else 'UNKNOWN'}"
                f" / 核心可用性 {rec.core_readiness if rec.core_readiness is not None else 'UNKNOWN'}；"
                f"全局风险 {rec.overall if rec.overall is not None else 'UNKNOWN'}"
                f"{'（下界）' if rec.score_is_lower_bound else ''}——仅作背景）",
                f"  (decided by {rec.decided_by}; your risk: personal impact "
                f"{rec.personal_impact if rec.personal_impact is not None else 'UNKNOWN'} / core readiness "
                f"{rec.core_readiness if rec.core_readiness is not None else 'UNKNOWN'}; global risk "
                f"{rec.overall if rec.overall is not None else 'UNKNOWN'}"
                f"{' (lower bound)' if rec.score_is_lower_bound else ''} - background only)",
            )
        )
        cautions = rec.cautions_zh if self.lang == "zh" else rec.cautions_en
        if cautions:
            console.blank()
            console.print(self._t("  提示（不影响是否可更新）：", "  Cautions (these do not block):"))
            for item in _dedupe(cautions)[:5]:
                for index, line in enumerate(_wrap(item, width=86)):
                    console.print(("    - " if index == 0 else "      ") + line)
        reasons = rec.reasons_zh if self.lang == "zh" else rec.reasons_en
        if reasons:
            console.blank()
            console.print(self._t("  原因：", "  Why:"))
            for reason in _dedupe(reasons)[:8]:
                for index, line in enumerate(_wrap(reason, width=86)):
                    console.print(("    - " if index == 0 else "      ") + line)
        remediation = rec.remediation_zh if self.lang == "zh" else rec.remediation_en
        if remediation:
            console.blank()
            console.print(self._t("  下一步：", "  Next steps:"))
            for item in _dedupe(remediation)[:5]:
                console.print(f"    - {item}")
        if rec.recheck_at is not None and rec.recheck_hours is not None:
            console.blank()
            console.print(
                self._t(
                    f"  下次监控复查 Next Monitoring Check：{iso(rec.recheck_at)}（约 {rec.recheck_hours:g} 小时后）",
                    f"  Next Monitoring Check: {iso(rec.recheck_at)} (~{rec.recheck_hours:g} hours later)",
                )
            )
            reason = rec.recheck_reason_zh if self.lang == "zh" else rec.recheck_reason_en
            if reason:
                console.print(f"    {reason}")
        if rec.policy_clearance_at is not None and rec.policy_clearance_hours is not None:
            console.print(
                self._t(
                    f"  策略最早放行 Earliest Policy Clearance：{iso(rec.policy_clearance_at)}"
                    f"（约 {rec.policy_clearance_hours:g} 小时后，届时发布年龄不再触发谨慎/阻断）",
                    f"  Earliest Policy Clearance: {iso(rec.policy_clearance_at)} "
                    f"(~{rec.policy_clearance_hours:g} hours later; the release age stops cautioning/blocking)",
                )
            )
        if rec.recheck_hours is not None:
            console.print(
                self._t(
                    "  （复查时间只是下一次观察的时机，不代表届时一定能更新）",
                    "  (the recheck time is when to look again, not a promise that the update becomes possible)",
                )
            )
        if rec.action in {RECOMMEND_SAFE, RECOMMEND_ACCEPTABLE}:
            console.blank()
            console.print("  hermes-update-check update --dry-run   " + self._t("# 先看计划", "# show the plan"))
            console.print(
                "  hermes-update-check update             "
                + self._t("# 备份 + 更新 + 健康检查", "# backup + update + health check")
            )
        if rec.action in {RECOMMEND_BLOCKED, RECOMMEND_WAIT, RECOMMEND_UNKNOWN}:
            console.blank()
            console.print("  hermes-update-check check              " + self._t("# 稍后再看", "# look again later"))
        console.blank()

    def _render_issue_samples(self, check: UpdateCheck) -> None:
        signal = check.issues
        if signal is None:
            return
        console = self.console
        if not signal.available:
            console.warn(
                self._t(
                    f"GitHub Issues 未能检查：{signal.unavailable_reason}（不等于安全）",
                    f"GitHub issues could not be checked: {signal.unavailable_reason} (not the same as safe)",
                )
            )
            return
        console.print(
            self._t(
                f"发布后 Issue 关键词命中：{signal.post_release_total} 个（扫描 {signal.scanned_items} 条）"
                + (
                    f"；基线窗口（{signal.baseline_window_days:.0f} 天前）：{signal.baseline_total} 个"
                    if signal.baseline_total is not None
                    else ""
                ),
                f"Issues matching risk keywords since the release: {signal.post_release_total} "
                f"(scanned {signal.scanned_items})"
                + (
                    f"; baseline window ({signal.baseline_window_days:.0f}d before): {signal.baseline_total}"
                    if signal.baseline_total is not None
                    else ""
                ),
            )
        )
        console.blank()

    def _render_local_block(self, check: UpdateCheck, *, detailed: bool = False) -> None:
        env = check.env
        console = self.console
        console.heading(self._section(SECTION_ENV))
        rows = [
            ("HERMES_HOME", str(env.hermes_home)),
            (self._t("安装方式", "Install method"), env.install_kind),
            (self._t("安装目录", "Install dir"), str(env.install_dir) if env.install_dir else "-"),
        ]
        if env.python_version:
            rows.append(("Python", env.python_version))
        if env.venv_python:
            rows.append(("venv python", str(env.venv_python)))
        if env.uv_path:
            rows.append(("uv", env.uv_path))
        console.kv_table(rows)
        console.blank()

    def _render_notes(self, check: UpdateCheck) -> None:
        console = self.console
        notes: list[str] = []
        prov = check.provenance
        if prov is not None:
            for zh, en in prov.evidence:
                notes.append(zh if self.lang == "zh" else en)
        if check.versions_behind is not None and check.versions_behind > 1:
            notes.append(
                self._t(
                    f"当前落后 {check.versions_behind} 个正式 Release（建议不要一次跨太多版本）",
                    f"{check.versions_behind} releases behind (avoid jumping across too many at once)",
                )
            )
        for item in check.degradation:
            notes.append(self._t(f"数据降级：{item}", f"degraded data: {item}"))
        if check.rate_limit:
            core = (check.rate_limit.get("resources") or {}).get("core") or {}
            if core:
                notes.append(
                    self._t(
                        f"GitHub API 剩余配额：{core.get('remaining')}/{core.get('limit')}"
                        + ("（已使用 Token）" if check.token_used else "（未使用 Token，建议设置 GITHUB_TOKEN）"),
                        f"GitHub API remaining: {core.get('remaining')}/{core.get('limit')}"
                        + (" (token in use)" if check.token_used else " (no token - set GITHUB_TOKEN)"),
                    )
                )
        notes.append(self._t(f"生成时间：{iso(check.generated_at)}", f"generated at: {iso(check.generated_at)}"))
        if notes:
            console.heading(self._section(SECTION_NOTES))
            for note in _dedupe(notes)[:14]:
                console.print(f"  - {note}")
            console.blank()

    # -- legacy API kept for compatibility ----------------------------------- #

    def recommendation_lines(self, check: UpdateCheck, assessment: Optional[RiskAssessment]) -> list[str]:
        """Natural-language advice.

        Prefers the advisor's recommendation (which already accounts for gates);
        falls back to the assessment when no advisor ran (older callers/tests).
        """
        rec = check.recommendation
        if rec is not None:
            lines = [rec.headline_zh if self.lang == "zh" else rec.headline_en]
            lines.extend(f"  - {r}" for r in (rec.reasons_zh if self.lang == "zh" else rec.reasons_en)[:5])
            if rec.action == RECOMMEND_WAIT:
                if rec.recheck_at is not None and rec.recheck_hours is not None:
                    lines.append(
                        self._t(
                            f"暂时不要更新，继续观察。建议复查时间：{iso(rec.recheck_at)}（约 {rec.recheck_hours:g} 小时后）。",
                            f"Do not update yet - keep observing. Recommended recheck: {iso(rec.recheck_at)} "
                            f"(~{rec.recheck_hours:g} hours later).",
                        )
                    )
                else:
                    lines.append(
                        self._t(
                            "暂时不要更新，继续观察一下再决定。",
                            "Do not update yet - keep observing before deciding.",
                        )
                    )
            elif rec.action in {RECOMMEND_SAFE, RECOMMEND_ACCEPTABLE}:
                lines.append(self._t("可以更新，但请先执行完整备份。", "You may update - take a full backup first."))
            else:
                # BLOCKED / INSUFFICIENT_DATA / MANUAL_REVIEW / AHEAD_OF_STABLE
                lines.append(
                    self._t(
                        "不要更新，继续观察（原因见上）。",
                        "Do not update - keep observing (see the reasons above).",
                    )
                )
                if rec.recheck_at is not None and rec.recheck_hours is not None:
                    lines.append(
                        self._t(
                            f"下次观察时间：{iso(rec.recheck_at)}（约 {rec.recheck_hours:g} 小时后）。",
                            f"Next check: {iso(rec.recheck_at)} (~{rec.recheck_hours:g} hours later).",
                        )
                    )
            return lines

        if assessment is None:
            return [self._t("无评估数据", "no assessment data")]

        if check.update_available is False:
            return [
                self._t("当前版本已是最新正式版本，无需任何操作。", "Already on the newest release - nothing to do.")
            ]

        rec_action = assessment.recommendation
        lines: list[str] = []
        if rec_action == RECOMMEND_UNKNOWN:
            lines.append(
                self._t(
                    "信息不足，无法给出可信风险评估 —— 请手动查看 Release Notes 与 Issues 后再决定。",
                    "Not enough information for a trustworthy assessment - review the release notes and issues manually.",
                )
            )
            return lines
        if rec_action in {RECOMMEND_SAFE, RECOMMEND_ACCEPTABLE, "UPDATE"}:
            lines.append(self._t("风险可接受，可以更新。", "Risk is acceptable - you may update."))
            lines.append(
                self._t("但请先做完整备份（不要跳过备份步骤）。", "Take a full backup first (do not skip it).")
            )
            return lines
        if rec_action in {RECOMMEND_BLOCKED, "AVOID"}:
            lines.append(self._t("强烈不建议更新：风险评分进入最高区间。", "Strongly not recommended: top risk band."))
            lines.append(
                self._t(
                    "建议等待下一个修复版本（通常是 1-2 个 patch 之后），并持续观察 Issues。",
                    "Wait for the next patch release (usually 1-2 patches later) and keep watching the issues.",
                )
            )
            return lines
        # WAIT
        lines.append(self._t("暂时不要更新。", "Do not update yet."))
        reasons = assessment.summary_zh if self.lang == "zh" else assessment.summary_en
        if reasons:
            lines.append(self._t("原因：", "Reasons:"))
            lines.extend(f"  - {r}" for r in reasons[:5])
        recheck = assessment.recheck_in_days or 3
        lines.append(
            self._t(
                f"建议继续观察 {recheck} 天，如果没有新的严重 Issue，再考虑升级。",
                f"Watch for {recheck} more day(s); if no new severe issues appear, revisit the update.",
            )
        )
        return lines

    # -- machine readable ---------------------------------------------------- #

    def to_markdown(self, check: UpdateCheck) -> str:
        lines: list[str] = [f"# {REPORT_TITLE}", ""]
        prov = check.provenance
        lines.append(f"## {self._section(SECTION_LOCAL)}")
        lines.append("")
        if prov is not None:
            lines.append(
                f"- **Reported Version**: v{prov.reported_version}"
                if prov.reported_version
                else "- **Reported Version**: unknown"
            )
            lines.append(f"- **Channel**: {prov.channel}")
            if prov.git_branch:
                lines.append(f"- **Branch**: {prov.git_branch}")
            if prov.git_commit:
                lines.append(f"- **Commit**: {prov.git_commit}")
            if prov.nearest_tag:
                lines.append(f"- **Nearest Release**: {prov.nearest_tag}")
            if prov.commits_ahead_of_tag is not None:
                lines.append(f"- **Ahead of Release**: {prov.commits_ahead_of_tag} commits")
            if prov.commits_behind_target is not None:
                lines.append(f"- **Behind Latest**: {prov.commits_behind_target} commits")
            if prov.is_git_install:
                lines.append(f"- **Working Tree**: {'dirty' if prov.dirty_worktree else 'clean'}")
        lines.append("")

        lines.append(f"## {self._section(SECTION_RELEASE)}")
        lines.append("")
        if check.latest is not None:
            lines.append(f"- **Release**: v{check.latest.display_version} ({check.latest.tag})")
            if check.latest.age_hours is not None:
                lines.append(
                    f"- **Published**: {iso(check.latest.when)} ({humanize_hours(check.latest.age_hours, lang=self.lang)}"
                    + self._t("前", " ago")
                    + ")"
                )
            if check.compare is not None:
                lines.append(f"- **Commits**: {check.compare.total_commits}")
            if check.pr_count is not None:
                lines.append(f"- **Merged PRs**: {check.pr_count}")
        else:
            lines.append("- no release information available")
        lines.append("")

        assessment = check.assessment
        if assessment is not None:
            lines.append(f"## {self._section(SECTION_RISK)}")
            lines.append("")
            for label, value in assessment.component_rows():
                lines.append(f"- **{label}**: {value}")
            if check.issues is not None and check.issues.signal_confidence is not None:
                lines.append(f"- **Issue Signal Confidence**: {check.issues.signal_confidence}/100")
            lines.append("")

        if check.gates is not None and check.gates.enabled:
            lines.append(f"## {self._section(SECTION_GATES)}")
            lines.append("")
            for gate in check.gates.gates:
                if gate.status == GATE_SKIP:
                    continue
                label = gate.name_zh if self.lang == "zh" else gate.name_en
                reason = gate.reason_zh if self.lang == "zh" else gate.reason_en
                suffix = f" — {reason}" if reason and gate.status != GATE_PASS else ""
                lines.append(f"- **{gate.status}** {label}{suffix}")
            lines.append("")

        if check.clusters:
            lines.append(f"## {self._section(SECTION_REGRESSIONS)}")
            lines.append("")
            lines.append("| cluster | severity | confidence | reports | reporters | open | confirmed |")
            lines.append("|---|---|---|---:|---:|---:|---:|")
            for cluster in check.clusters:
                label = cluster.en if self.lang == "en" else cluster.zh
                lines.append(
                    f"| {label} | {cluster.severity} | {cluster.confidence} | {cluster.reports} | "
                    f"{cluster.unique_reporters} | {cluster.open_count} | {cluster.maintainer_confirmed} |"
                )
            lines.append("")

        if assessment is not None:
            lines.append(f"## {self._section(SECTION_FACTORS)}")
            lines.append("")
            lines.append("| factor | pts | detail |")
            lines.append("|---|---:|---|")
            for factor in assessment.factors:
                label = factor.label_en if self.lang == "en" else factor.label_zh
                detail = (factor.detail_en if self.lang == "en" else factor.detail_zh).replace("|", "/")
                lines.append(f"| {label} | {factor.points:+.1f} | {detail} |")
            lines.append("")

        lines.append(f"## {self._section(SECTION_RECOMMENDATION)}")
        lines.append("")
        rec = check.recommendation
        if rec is not None:
            lines.append(f"**{rec.action}**")
            lines.append("")
            lines.append(rec.headline_zh if self.lang == "zh" else rec.headline_en)
            lines.append("")
            for reason in (rec.reasons_zh if self.lang == "zh" else rec.reasons_en)[:8]:
                lines.append(f"- {reason}")
            if rec.recheck_at is not None and rec.recheck_hours is not None:
                lines.append("")
                lines.append(
                    self._t(
                        f"建议复查时间：{iso(rec.recheck_at)}（约 {rec.recheck_hours:g} 小时后）",
                        f"Recommended recheck: {iso(rec.recheck_at)} (~{rec.recheck_hours:g} h later)",
                    )
                )
        else:
            for line in self.recommendation_lines(check, assessment):
                lines.append(line)
        lines.append("")

        if check.degradation:
            lines.append(f"## {self._section(SECTION_NOTES)}")
            lines.append("")
            for item in check.degradation:
                lines.append(f"- {item}")
        return "\n".join(lines) + "\n"


def _wrap(text: str, *, width: int = 92) -> list[str]:
    """Wrap long single-line messages (CJK-safe enough: counts characters)."""
    if len(text) <= width:
        return [text]
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
