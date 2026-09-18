"""Pre-update checks: is this machine actually in a state where an update is safe to start?

Nothing here changes anything - every check is read-only. ``FAIL`` blocks the
update, ``WARN`` is recorded and surfaced but does not block.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import Config, resolve_state_dir
from .local_env import LocalEnv, gateway_status, list_hermes_processes, parse_gateway_status
from .logging_setup import get_logger
from .util import ProcResult, disk_free_bytes, human_bytes, run_process

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"

MIN_ABSOLUTE_FREE_BYTES = 500 * 1024 * 1024  # below this an update can genuinely fail


@dataclass
class CheckResult:
    key: str
    name_zh: str
    name_en: str
    status: str
    detail_zh: str = ""
    detail_en: str = ""
    remediation_zh: str = ""
    remediation_en: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == STATUS_FAIL

    @property
    def ok(self) -> bool:
        return self.status in {STATUS_PASS, STATUS_SKIP}

    def name(self, lang: str = "zh") -> str:
        return self.name_zh if lang == "zh" else self.name_en

    def detail(self, lang: str = "zh") -> str:
        return self.detail_zh if lang == "zh" else self.detail_en

    def remediation(self, lang: str = "zh") -> str:
        return self.remediation_zh if lang == "zh" else self.remediation_en

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "detail": self.detail_en,
            "remediation": self.remediation_en,
        }


@dataclass
class PreflightReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == STATUS_FAIL]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == STATUS_WARN]

    @property
    def passed(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == STATUS_PASS]

    @property
    def blocking_failures(self) -> list[CheckResult]:
        return [c for c in self.failures if c.blocking]

    @property
    def ok_to_proceed(self) -> bool:
        return not self.blocking_failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok_to_proceed": self.ok_to_proceed,
            "failures": [c.to_dict() for c in self.failures],
            "warnings": [c.to_dict() for c in self.warnings],
            "checks": [c.to_dict() for c in self.checks],
        }


def run_preflight(
    cfg: Config,
    env: LocalEnv,
    *,
    state_root: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
    check_processes: bool = True,
    update_help_text: Optional[str] = None,
) -> PreflightReport:
    """Run every pre-update check. Read-only; safe on a live system."""
    log = logger or get_logger("preflight")
    root = state_root or resolve_state_dir(cfg)
    report = PreflightReport()

    report.checks.append(_check_hermes_home(env))
    report.checks.append(_check_config_file(env))
    report.checks.append(_check_min_free_disk(cfg, env))
    report.checks.append(_check_state_dir(root))
    report.checks.append(_check_version_known(env))
    report.checks.append(_check_install_method(env))
    report.checks.append(_check_git_clean(env))
    report.checks.append(_check_venv(env))
    report.checks.append(_check_state_db(env))
    report.checks.append(_check_backup_support(env, update_help_text=update_help_text))
    report.checks.extend(_check_overrides(cfg, env, root))
    if check_processes:
        report.checks.append(_check_running_processes(log))
        report.checks.append(_check_gateway(env))

    log.debug("preflight: %s", [f"{c.key}={c.status}" for c in report.checks])
    return report


# --------------------------------------------------------------------------- #
# phase 4: managed local overrides
# --------------------------------------------------------------------------- #


def _check_overrides(cfg: Config, env: LocalEnv, root: Path) -> list[CheckResult]:
    """Local customization integrity: what an update will preserve.

    Doc section 45: managed override integrity, unknown dirty files, patch
    writability, git availability, base/target reachability.
    """
    from .overrides import (
        SAFETY_FAIL,
        git_available,
        integrity_issues,
        load_registry,
        patches_dir,
    )
    from .overrides import (
        classify as classify_overrides,
    )

    results: list[CheckResult] = []
    registry = load_registry(root)
    if registry.broken:
        results.append(
            CheckResult(
                key="override_registry",
                name_zh="本地定制登记表",
                name_en="override registry",
                status=STATUS_FAIL,
                detail_zh=f"登记表损坏：{registry.broken_reason}",
                detail_en=f"the registry is broken: {registry.broken_reason}",
                remediation_zh="先修复或删除 overrides/registry.json（未登记修改会被视为意外改动）",
                remediation_en="repair or remove overrides/registry.json (unregistered changes count as accidents)",
            )
        )
        return results

    if registry.empty:
        results.append(
            CheckResult(
                key="override_registry",
                name_zh="本地定制登记表",
                name_en="override registry",
                status=STATUS_PASS,
                detail_zh="没有登记的本地定制",
                detail_en="no managed local overrides",
            )
        )
        return results

    base = (registry.base_commit or "unknown")[:8]
    results.append(
        CheckResult(
            key="override_registry",
            name_zh="本地定制登记表",
            name_en="override registry",
            status=STATUS_PASS,
            detail_zh=f"{len(registry.files)} 个已登记定制（base {base}）",
            detail_en=f"{len(registry.files)} managed override(s) (base {base})",
        )
    )

    if env.install_dir is None or not git_available(install_dir=env.install_dir):
        results.append(
            CheckResult(
                key="override_git",
                name_zh="git 可用性（定制保全）",
                name_en="git availability (override preservation)",
                status=STATUS_FAIL,
                detail_zh="找不到可用的 git：无法保全/恢复本地定制",
                detail_en="no working git executable: local overrides cannot be preserved or restored",
                remediation_zh="安装 git 后再更新，或先手动备份你的定制",
                remediation_en="install git before updating, or back up your customization manually",
            )
        )
        return results

    report = classify_overrides(env.install_dir, registry)
    if report.unknown_count:
        detail_zh = f"{report.unknown_count} 个未登记修改：更新会被阻断"
        detail_en = f"{report.unknown_count} unregistered change(s): an update will be blocked"
    else:
        detail_zh = "没有未登记修改"
        detail_en = "no unregistered changes"
    results.append(
        CheckResult(
            key="override_unknown",
            name_zh="未登记修改",
            name_en="unregistered changes",
            status=STATUS_FAIL if report.safety == SAFETY_FAIL else STATUS_PASS,
            detail_zh=detail_zh,
            detail_en=detail_en,
            remediation_zh="登记（huc overrides register）或提交/stash/删除它们",
            remediation_en="register them (`huc overrides register`) or commit/stash/remove them",
        )
    )
    if report.drifted:
        results.append(
            CheckResult(
                key="override_drift",
                name_zh="定制漂移",
                name_en="override drift",
                status=STATUS_WARN,
                detail_zh=f"{report.drifted_count} 个定制在登记后又被改过",
                detail_en=f"{report.drifted_count} override(s) changed again after registration",
                remediation_zh="确认后运行 huc overrides refresh",
                remediation_en="review them, then run `huc overrides refresh`",
            )
        )
    writable = True
    try:
        directory = patches_dir(root)
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        writable = False
    results.append(
        CheckResult(
            key="override_patch_writable",
            name_zh="patch 目录可写",
            name_en="patch directory writable",
            status=STATUS_PASS if writable else STATUS_FAIL,
            detail_zh="可以写入 overrides/patches" if writable else "无法写入 overrides/patches",
            detail_en="overrides/patches is writable" if writable else "overrides/patches is NOT writable",
            remediation_zh="检查状态目录权限",
            remediation_en="check the permissions of the state directory",
        )
    )
    for issue in [i for i in integrity_issues(root, env.install_dir) if i.severity in {"fail", "unknown"}][:3]:
        results.append(
            CheckResult(
                key=issue.key,
                name_zh="定制完整性",
                name_en="override integrity",
                status=STATUS_FAIL if issue.severity == "fail" else STATUS_WARN,
                detail_zh=issue.message_zh,
                detail_en=issue.message_en,
                remediation_zh="运行 huc overrides doctor 查看详情",
                remediation_en="run `huc overrides doctor` for details",
            )
        )
    return results


# --------------------------------------------------------------------------- #
# individual checks
# --------------------------------------------------------------------------- #


def _check_hermes_home(env: LocalEnv) -> CheckResult:
    if env.hermes_home.exists():
        return CheckResult(
            key="hermes_home",
            name_zh="HERMES_HOME 是否存在",
            name_en="HERMES_HOME exists",
            status=STATUS_PASS,
            detail_zh=str(env.hermes_home),
            detail_en=str(env.hermes_home),
        )
    return CheckResult(
        key="hermes_home",
        name_zh="HERMES_HOME 是否存在",
        name_en="HERMES_HOME exists",
        status=STATUS_FAIL,
        detail_zh=f"目录不存在: {env.hermes_home}",
        detail_en=f"directory missing: {env.hermes_home}",
        remediation_zh="设置 HERMES_HOME 或在 config.yaml 里指定 paths.hermes_home",
        remediation_en="set HERMES_HOME or paths.hermes_home in the config file",
    )


def _check_config_file(env: LocalEnv) -> CheckResult:
    path = env.config_path
    if not path.exists():
        return CheckResult(
            key="config_file",
            name_zh="config.yaml 可读性",
            name_en="config.yaml readable",
            status=STATUS_WARN,
            detail_zh=f"未找到 {path}",
            detail_en=f"{path} not found",
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return CheckResult(
            key="config_file",
            name_zh="config.yaml 可读性",
            name_en="config.yaml readable",
            status=STATUS_FAIL,
            detail_zh=f"无法读取: {exc}",
            detail_en=f"unreadable: {exc}",
            remediation_zh="检查文件权限（应为运行 Hermes 的用户可读）",
            remediation_en="check file permissions (must be readable by the Hermes user)",
        )
    if not text.strip():
        return CheckResult(
            key="config_file",
            name_zh="config.yaml 可读性",
            name_en="config.yaml readable",
            status=STATUS_WARN,
            detail_zh="文件为空",
            detail_en="file is empty",
        )
    return CheckResult(
        key="config_file",
        name_zh="config.yaml 可读性",
        name_en="config.yaml readable",
        status=STATUS_PASS,
        detail_zh=f"{len(text)} bytes",
        detail_en=f"{len(text)} bytes",
    )


def _check_min_free_disk(cfg: Config, env: LocalEnv) -> CheckResult:
    target = env.hermes_home if env.hermes_home.exists() else Path.home()
    free = disk_free_bytes(target)
    required = int(cfg.update.min_free_disk_gib * 1024**3)
    if free is None:
        return CheckResult(
            key="disk_space",
            name_zh="剩余磁盘空间",
            name_en="Free disk space",
            status=STATUS_WARN,
            detail_zh="无法确定剩余空间",
            detail_en="could not determine free space",
        )
    if free < MIN_ABSOLUTE_FREE_BYTES:
        return CheckResult(
            key="disk_space",
            name_zh="剩余磁盘空间",
            name_en="Free disk space",
            status=STATUS_FAIL,
            detail_zh=f"仅剩 {human_bytes(free)}（低于 500 MiB 安全线）",
            detail_en=f"only {human_bytes(free)} left (below the 500 MiB safety line)",
            remediation_zh="清理空间后重试（更新过程需要下载依赖 + 生成备份）",
            remediation_en="free up space and retry (update downloads deps and writes a backup)",
        )
    if free < required:
        return CheckResult(
            key="disk_space",
            name_zh="剩余磁盘空间",
            name_en="Free disk space",
            status=STATUS_WARN,
            detail_zh=f"剩余 {human_bytes(free)}，低于建议值 {cfg.update.min_free_disk_gib:g} GiB",
            detail_en=f"{human_bytes(free)} free, below the recommended {cfg.update.min_free_disk_gib:g} GiB",
        )
    return CheckResult(
        key="disk_space",
        name_zh="剩余磁盘空间",
        name_en="Free disk space",
        status=STATUS_PASS,
        detail_zh=f"剩余 {human_bytes(free)}",
        detail_en=f"{human_bytes(free)} free",
    )


def _check_state_dir(root: Path) -> CheckResult:
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            key="state_dir",
            name_zh="状态目录可写",
            name_en="State directory writable",
            status=STATUS_FAIL,
            detail_zh=f"{root}: {exc}",
            detail_en=f"{root}: {exc}",
            remediation_zh="设置 HERMES_UPDATE_CHECK_STATE_DIR 到一个可写目录",
            remediation_en="point HERMES_UPDATE_CHECK_STATE_DIR at a writable location",
        )
    return CheckResult(
        key="state_dir",
        name_zh="状态目录可写",
        name_en="State directory writable",
        status=STATUS_PASS,
        detail_zh=str(root),
        detail_en=str(root),
    )


def _check_version_known(env: LocalEnv) -> CheckResult:
    if env.version or env.commit:
        return CheckResult(
            key="version_known",
            name_zh="当前版本可识别",
            name_en="Current version identified",
            status=STATUS_PASS,
            detail_zh=env.version_label,
            detail_en=env.version_label,
        )
    return CheckResult(
        key="version_known",
        name_zh="当前版本可识别",
        name_en="Current version identified",
        status=STATUS_WARN,
        detail_zh="无法确定当前版本（hermes --version 失败）",
        detail_en="could not determine the current version (hermes --version failed)",
        remediation_zh="先修复本地 Hermes 安装再更新",
        remediation_en="fix the local Hermes installation before updating",
    )


def _check_install_method(env: LocalEnv) -> CheckResult:
    if env.install_kind == "unknown":
        return CheckResult(
            key="install_method",
            name_zh="安装方式可识别",
            name_en="Install method identified",
            status=STATUS_WARN,
            detail_zh="无法判断安装方式（git / pip / docker）",
            detail_en="could not determine the install method (git / pip / docker)",
            remediation_zh="确认安装方式后再更新，否则回滚路径不明确",
            remediation_en="confirm the install method first - otherwise the rollback path is unclear",
        )
    return CheckResult(
        key="install_method",
        name_zh="安装方式可识别",
        name_en="Install method identified",
        status=STATUS_PASS,
        detail_zh=env.install_kind,
        detail_en=env.install_kind,
    )


def _check_git_clean(env: LocalEnv) -> CheckResult:
    if not env.git or not env.git.is_repo:
        return CheckResult(
            key="git_clean",
            name_zh="Git 工作区是否干净",
            name_en="Git worktree clean",
            status=STATUS_SKIP,
            detail_zh="非 git 安装",
            detail_en="not a git install",
        )
    if env.git.dirty:
        files = ", ".join(env.git.dirty_files[:5])
        return CheckResult(
            key="git_clean",
            name_zh="Git 工作区是否干净",
            name_en="Git worktree clean",
            status=STATUS_WARN,
            detail_zh=f"{len(env.git.dirty_files)} 个未提交修改: {files}",
            detail_en=f"{len(env.git.dirty_files)} uncommitted change(s): {files}",
            remediation_zh="先提交或 stash 本地修改；若是有意保留的定制，用 `huc overrides register` 登记（见下方本地定制检查）",
            remediation_en="commit or stash them; if they are intentional customizations, register them with "
            "`huc overrides register` (see the local-customization checks below)",
        )
    return CheckResult(
        key="git_clean",
        name_zh="Git 工作区是否干净",
        name_en="Git worktree clean",
        status=STATUS_PASS,
        detail_zh=f"{env.git.branch} @ {env.git.commit}",
        detail_en=f"{env.git.branch} @ {env.git.commit}",
    )


def _check_venv(env: LocalEnv) -> CheckResult:
    if env.venv_python and env.venv_python.exists():
        return CheckResult(
            key="venv",
            name_zh="Python 环境",
            name_en="Python environment",
            status=STATUS_PASS,
            detail_zh=str(env.venv_python),
            detail_en=str(env.venv_python),
        )
    return CheckResult(
        key="venv",
        name_zh="Python 环境",
        name_en="Python environment",
        status=STATUS_WARN,
        detail_zh="未找到 venv python（回滚时的依赖重装需要它）",
        detail_en="venv python not found (needed to reinstall dependencies on rollback)",
    )


def _check_state_db(env: LocalEnv) -> CheckResult:
    path = env.state_db
    if not path.exists():
        return CheckResult(
            key="state_db",
            name_zh="Session 数据库存在且可读",
            name_en="Session database present and readable",
            status=STATUS_WARN,
            detail_zh=f"未找到 {path}（首次启动时会自动创建）",
            detail_en=f"{path} not found (created on first run)",
        )
    size = human_bytes(path.stat().st_size) if path.stat else "?"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
            conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        return CheckResult(
            key="state_db",
            name_zh="Session 数据库存在且可读",
            name_en="Session database present and readable",
            status=STATUS_FAIL,
            detail_zh=f"SQLite 读取失败: {exc}",
            detail_en=f"SQLite read failed: {exc}",
            remediation_zh="先用 `hermes doctor` 检查数据库；数据库异常时不要执行更新",
            remediation_en="run `hermes doctor` first; do not update while the database is unhealthy",
        )
    return CheckResult(
        key="state_db",
        name_zh="Session 数据库存在且可读",
        name_en="Session database present and readable",
        status=STATUS_PASS,
        detail_zh=f"{size}, quick_check OK",
        detail_en=f"{size}, quick_check OK",
    )


def _check_backup_support(env: LocalEnv, *, update_help_text: Optional[str] = None) -> CheckResult:
    help_text = update_help_text
    if help_text is None and env.hermes_cli:
        result = run_process([env.hermes_cli, "update", "--help"], timeout=45.0)
        help_text = result.output
    if not help_text:
        return CheckResult(
            key="backup_support",
            name_zh="hermes update 备份能力",
            name_en="hermes update backup support",
            status=STATUS_WARN,
            detail_zh="无法读取 `hermes update --help`",
            detail_en="could not read `hermes update --help`",
        )
    if "--backup" in help_text:
        return CheckResult(
            key="backup_support",
            name_zh="hermes update 备份能力",
            name_en="hermes update backup support",
            status=STATUS_PASS,
            detail_zh="支持 --backup（会生成 HERMES_HOME 完整备份）",
            detail_en="supports --backup (full HERMES_HOME backup)",
        )
    return CheckResult(
        key="backup_support",
        name_zh="hermes update 备份能力",
        name_en="hermes update backup support",
        status=STATUS_WARN,
        detail_zh="当前版本不支持 --backup，请手动备份 HERMES_HOME",
        detail_en="this Hermes version has no --backup flag; back up HERMES_HOME manually",
    )


def _check_running_processes(log: logging.Logger) -> CheckResult:
    processes, error = list_hermes_processes()
    if error:
        return CheckResult(
            key="processes",
            name_zh="运行中的 Hermes 进程",
            name_en="Running Hermes processes",
            status=STATUS_WARN,
            detail_zh=f"无法枚举进程: {error}",
            detail_en=f"could not enumerate processes: {error}",
        )
    if not processes:
        return CheckResult(
            key="processes",
            name_zh="运行中的 Hermes 进程",
            name_en="Running Hermes processes",
            status=STATUS_PASS,
            detail_zh="未发现其他 Hermes 进程",
            detail_en="no other Hermes processes found",
        )
    samples = "; ".join(p.short for p in processes[:3])
    log.debug("running hermes processes: %s", samples)
    return CheckResult(
        key="processes",
        name_zh="运行中的 Hermes 进程",
        name_en="Running Hermes processes",
        status=STATUS_WARN,
        detail_zh=f"{len(processes)} 个进程正在运行: {samples}",
        detail_en=f"{len(processes)} process(es) running: {samples}",
        remediation_zh="更新会重启 Gateway / 中断当前会话；必要时先停掉会话再更新",
        remediation_en="the update restarts the gateway and interrupts sessions; stop them first if that matters",
    )


def _check_gateway(env: LocalEnv) -> CheckResult:
    result: ProcResult = gateway_status(env.hermes_cli)
    status, detail = parse_gateway_status(result)
    if status == STATUS_PASS:
        detail_zh = "Gateway 正在运行（更新过程中会被重启）"
        detail_en = "gateway is running (it will be restarted during the update)"
    else:
        detail_zh = f"Gateway 未运行或状态未知：{detail}"
        detail_en = f"gateway not running or unknown: {detail}"
    return CheckResult(
        key="gateway",
        name_zh="Gateway 状态",
        name_en="Gateway status",
        status=status,
        detail_zh=detail_zh,
        detail_en=detail_en,
    )
