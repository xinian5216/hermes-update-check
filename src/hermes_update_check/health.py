"""Post-update health checks: did the update actually leave a working install?

Seven independent probes, each PASS / WARN / FAIL:

1. ``hermes --version``          - the CLI starts and reports a version
2. ``import hermes_cli``         - the venv's python can import the package
3. ``config.yaml`` parses        - settings survived the update
4. ``state.db`` integrity        - SQLite opens, quick_check passes, sessions table readable
5. ``hermes gateway status``     - the gateway service answers
6. ``hermes doctor``             - the built-in self test
7. MCP / tools config loads      - mcp & tools sections still parse, ``hermes mcp list`` answers

Anything FAIL means "UPDATE FAILED HEALTH CHECK" and rollback is offered.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import Config
from .local_env import LocalEnv, gateway_status, parse_gateway_status
from .logging_setup import get_logger
from .preflight import STATUS_FAIL, STATUS_PASS, STATUS_SKIP, STATUS_WARN, CheckResult
from .util import run_process
from .versioning import parse_version_output

try:  # pragma: no cover - environment dependent
    import yaml

    _YAML_AVAILABLE = True
except Exception:  # pragma: no cover
    yaml = None  # type: ignore[assignment]
    _YAML_AVAILABLE = False


@dataclass
class HealthReport:
    checks: list[CheckResult] = field(default_factory=list)
    smoke_tested: bool = False

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == STATUS_FAIL]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == STATUS_WARN]

    @property
    def healthy(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "smoke_tested": self.smoke_tested,
            "failures": [c.to_dict() for c in self.failures],
            "warnings": [c.to_dict() for c in self.warnings],
            "checks": [c.to_dict() for c in self.checks],
        }

    def summary_line(self, *, lang: str = "zh") -> str:
        failed = len(self.failures)
        warned = len(self.warnings)
        passed = len([c for c in self.checks if c.status == STATUS_PASS])
        if lang == "zh":
            return f"通过 {passed}，警告 {warned}，失败 {failed}"
        return f"{passed} passed, {warned} warning(s), {failed} failure(s)"


def run_health_checks(
    cfg: Config,
    env: LocalEnv,
    *,
    state_root: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
    smoke_test: bool = False,
    timeout: float = 90.0,
    gateway_timeout: float = 45.0,
    gateway_was_running: Optional[bool] = None,
) -> HealthReport:
    """Run the full health suite. Read-only (``doctor`` is called without --fix``).

    ``gateway_was_running`` makes the gateway probe *comparative*: a gateway that
    was running before the update and is not running now is a FAIL, while a
    machine that never ran a gateway only gets a WARN (desktop-only installs).
    """
    log = logger or get_logger("health")
    report = HealthReport(smoke_tested=smoke_test)

    report.checks.append(_check_version(env, timeout=timeout))
    report.checks.append(_check_import(env, timeout=timeout))
    report.checks.append(_check_config(env))
    report.checks.append(_check_state_db(env))
    report.checks.append(_check_gateway(env, timeout=gateway_timeout, was_running=gateway_was_running))
    report.checks.append(_check_doctor(env, timeout=max(timeout, 180.0)))
    report.checks.append(_check_mcp_tools(env, timeout=timeout))
    if smoke_test:
        report.checks.append(_check_smoke(env, timeout=180.0))

    log.debug("health: %s", [f"{c.key}={c.status}" for c in report.checks])
    return report


# --------------------------------------------------------------------------- #
# probes
# --------------------------------------------------------------------------- #


def _check_version(env: LocalEnv, *, timeout: float) -> CheckResult:
    if not env.hermes_cli:
        return CheckResult(
            key="cli_version",
            name_zh="hermes CLI 是否可启动",
            name_en="hermes CLI starts",
            status=STATUS_FAIL,
            detail_zh="找不到 hermes 可执行文件",
            detail_en="hermes executable not found",
            remediation_zh="检查 PATH，或重新安装 Hermes",
            remediation_en="check PATH, or reinstall Hermes",
        )
    result = run_process([env.hermes_cli, "--version"], timeout=timeout)
    parsed = parse_version_output(result.output)
    if result.ok and parsed.parsed:
        return CheckResult(
            key="cli_version",
            name_zh="hermes CLI 是否可启动",
            name_en="hermes CLI starts",
            status=STATUS_PASS,
            detail_zh=f"v{parsed.version}" + (f" ({parsed.release_tag})" if parsed.release_tag else ""),
            detail_en=f"v{parsed.version}" + (f" ({parsed.release_tag})" if parsed.release_tag else ""),
        )
    return CheckResult(
        key="cli_version",
        name_zh="hermes CLI 是否可启动",
        name_en="hermes CLI starts",
        status=STATUS_FAIL,
        detail_zh=f"exit={result.returncode} {result.first_lines[:200]}",
        detail_en=f"exit={result.returncode} {result.first_lines[:200]}",
        remediation_zh="立即回滚：hermes-update-check rollback",
        remediation_en="roll back now: hermes-update-check rollback",
    )


def _check_import(env: LocalEnv, *, timeout: float) -> CheckResult:
    python = env.venv_python
    if python is None or not Path(python).exists():
        return CheckResult(
            key="python_import",
            name_zh="Python 依赖是否完整",
            name_en="Python imports work",
            status=STATUS_SKIP,
            detail_zh="未找到 venv python，无法验证 import",
            detail_en="venv python not found; cannot verify imports",
        )
    result = run_process(
        [str(python), "-c", "import hermes_cli, sys; print(hermes_cli.__version__); print(sys.version.split()[0])"],
        timeout=timeout,
        cwd=env.install_dir,
    )
    if result.ok:
        lines = result.stdout.strip().splitlines()
        version = lines[0] if lines else "?"
        py = lines[1] if len(lines) > 1 else "?"
        return CheckResult(
            key="python_import",
            name_zh="Python 依赖是否完整",
            name_en="Python imports work",
            status=STATUS_PASS,
            detail_zh=f"hermes_cli {version} / Python {py}",
            detail_en=f"hermes_cli {version} / Python {py}",
        )
    return CheckResult(
        key="python_import",
        name_zh="Python 依赖是否完整",
        name_en="Python imports work",
        status=STATUS_FAIL,
        detail_zh=f"import 失败: {(result.stderr or result.stdout).strip()[:300]}",
        detail_en=f"import failed: {(result.stderr or result.stdout).strip()[:300]}",
        remediation_zh='重装依赖后重试：uv pip install -e ".[all]"',
        remediation_en='reinstall dependencies: uv pip install -e ".[all]"',
    )


def _check_config(env: LocalEnv) -> CheckResult:
    path = env.config_path
    if not path.exists():
        return CheckResult(
            key="config_parse",
            name_zh="config.yaml 是否可解析",
            name_en="config.yaml parses",
            status=STATUS_WARN,
            detail_zh=f"未找到 {path}",
            detail_en=f"{path} not found",
        )
    if not _YAML_AVAILABLE:
        return CheckResult(
            key="config_parse",
            name_zh="config.yaml 是否可解析",
            name_en="config.yaml parses",
            status=STATUS_SKIP,
            detail_zh="未安装 PyYAML，跳过解析检查",
            detail_en="PyYAML not installed; parse check skipped",
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    except Exception as exc:
        return CheckResult(
            key="config_parse",
            name_zh="config.yaml 是否可解析",
            name_en="config.yaml parses",
            status=STATUS_FAIL,
            detail_zh=f"YAML 解析失败: {exc}",
            detail_en=f"YAML parse failed: {exc}",
            remediation_zh="从 backups/ 恢复 config.yaml，或运行 hermes config migrate 重新生成",
            remediation_en="restore config.yaml from backups/, or run hermes config migrate",
        )
    if not isinstance(data, dict):
        return CheckResult(
            key="config_parse",
            name_zh="config.yaml 是否可解析",
            name_en="config.yaml parses",
            status=STATUS_FAIL,
            detail_zh="顶层不是映射（文件可能被损坏）",
            detail_en="top level is not a mapping (file may be corrupted)",
        )
    return CheckResult(
        key="config_parse",
        name_zh="config.yaml 是否可解析",
        name_en="config.yaml parses",
        status=STATUS_PASS,
        detail_zh=f"{len(data)} 个顶层配置项",
        detail_en=f"{len(data)} top-level keys",
    )


def _check_state_db(env: LocalEnv) -> CheckResult:
    path = env.state_db
    if not path.exists():
        return CheckResult(
            key="state_db",
            name_zh="Session 数据库完整性",
            name_en="Session database integrity",
            status=STATUS_WARN,
            detail_zh=f"未找到 {path}（若之前也没有则属正常）",
            detail_en=f"{path} not found (normal if it never existed)",
        )
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10) as conn:
            integrity = conn.execute("PRAGMA quick_check").fetchone()
            ok = bool(integrity and integrity[0] == "ok")
            sessions = _count_sessions(conn)
    except sqlite3.Error as exc:
        return CheckResult(
            key="state_db",
            name_zh="Session 数据库完整性",
            name_en="Session database integrity",
            status=STATUS_FAIL,
            detail_zh=f"SQLite 错误: {exc}",
            detail_en=f"SQLite error: {exc}",
            remediation_zh="停止 Gateway 后回滚，或从备份恢复 state.db",
            remediation_en="stop the gateway and roll back, or restore state.db from a backup",
        )
    if not ok:
        return CheckResult(
            key="state_db",
            name_zh="Session 数据库完整性",
            name_en="Session database integrity",
            status=STATUS_FAIL,
            detail_zh="PRAGMA quick_check 未返回 ok",
            detail_en="PRAGMA quick_check did not return ok",
        )
    detail = "quick_check ok"
    if sessions is not None:
        detail += f", sessions={sessions}"
    return CheckResult(
        key="state_db",
        name_zh="Session 数据库完整性",
        name_en="Session database integrity",
        status=STATUS_PASS,
        detail_zh=detail,
        detail_en=detail,
    )


def _count_sessions(conn: sqlite3.Connection) -> Optional[int]:
    for table in ("sessions", "session", "conversations"):
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            if row:
                return int(row[0])
        except sqlite3.Error:
            continue
    return None


def _check_gateway(env: LocalEnv, *, timeout: float, was_running: Optional[bool] = None) -> CheckResult:
    result = gateway_status(env.hermes_cli, timeout=timeout)
    status, detail = parse_gateway_status(result)
    if status == STATUS_PASS:
        return CheckResult(
            key="gateway",
            name_zh="Gateway 是否正常",
            name_en="Gateway healthy",
            status=STATUS_PASS,
            detail_zh="正在运行",
            detail_en="running",
        )

    if was_running:
        # It answered before the update and does not answer now: real regression.
        return CheckResult(
            key="gateway",
            name_zh="Gateway 是否正常",
            name_en="Gateway healthy",
            status=STATUS_FAIL,
            detail_zh=f"更新前 Gateway 正在运行，现在无法确认：{detail}",
            detail_en=f"gateway was running before the update and is not now: {detail}",
            remediation_zh="查看 Gateway 日志；必要时 hermes gateway restart，仍失败则回滚",
            remediation_en="check the gateway logs; try `hermes gateway restart`, roll back if it still fails",
        )

    if result.error:
        return CheckResult(
            key="gateway",
            name_zh="Gateway 是否正常",
            name_en="Gateway healthy",
            status=STATUS_WARN,
            detail_zh=f"无法查询 Gateway 状态：{detail}",
            detail_en=f"could not query the gateway: {detail}",
        )
    return CheckResult(
        key="gateway",
        name_zh="Gateway 是否正常",
        name_en="Gateway healthy",
        status=STATUS_WARN,
        detail_zh="Gateway 未运行（此前也未运行，通常只影响消息平台接入）",
        detail_en="gateway is not running (it was not running before either; messaging platforms are affected only)",
    )


def _check_doctor(env: LocalEnv, *, timeout: float) -> CheckResult:
    if not env.hermes_cli:
        return CheckResult(
            key="doctor",
            name_zh="hermes doctor",
            name_en="hermes doctor",
            status=STATUS_SKIP,
            detail_zh="hermes 不可用",
            detail_en="hermes unavailable",
        )
    result = run_process([env.hermes_cli, "doctor"], timeout=timeout)
    output = result.output
    if result.timed_out:
        return CheckResult(
            key="doctor",
            name_zh="hermes doctor",
            name_en="hermes doctor",
            status=STATUS_WARN,
            detail_zh=f"超时（{timeout:g}s）",
            detail_en=f"timed out after {timeout:g}s",
        )
    problem_lines = [
        line.strip()
        for line in output.splitlines()
        if any(token in line for token in ("FAIL", "ERROR", "✗", "Traceback"))
    ]
    if problem_lines:
        return CheckResult(
            key="doctor",
            name_zh="hermes doctor",
            name_en="hermes doctor",
            status=STATUS_FAIL if result.returncode not in (0,) else STATUS_WARN,
            detail_zh="; ".join(problem_lines[:3])[:300],
            detail_en="; ".join(problem_lines[:3])[:300],
            remediation_zh="运行 `hermes doctor --fix` 后重新检查",
            remediation_en="run `hermes doctor --fix` and re-check",
        )
    if result.ok:
        return CheckResult(
            key="doctor",
            name_zh="hermes doctor",
            name_en="hermes doctor",
            status=STATUS_PASS,
            detail_zh="无错误报告",
            detail_en="no problems reported",
        )
    return CheckResult(
        key="doctor",
        name_zh="hermes doctor",
        name_en="hermes doctor",
        status=STATUS_WARN,
        detail_zh=f"exit={result.returncode}: {result.first_lines[:200]}",
        detail_en=f"exit={result.returncode}: {result.first_lines[:200]}",
    )


def _check_mcp_tools(env: LocalEnv, *, timeout: float) -> CheckResult:
    """MCP / tools configuration still loads (file level + `hermes mcp list` if available)."""
    path = env.config_path
    if not path.exists() or not _YAML_AVAILABLE:
        return CheckResult(
            key="mcp_tools",
            name_zh="MCP / 工具配置能否加载",
            name_en="MCP / tools config loads",
            status=STATUS_SKIP,
            detail_zh="config.yaml 不可读或 PyYAML 缺失",
            detail_en="config.yaml unreadable or PyYAML missing",
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}  # type: ignore[union-attr]
    except Exception:
        return CheckResult(
            key="mcp_tools",
            name_zh="MCP / 工具配置能否加载",
            name_en="MCP / tools config loads",
            status=STATUS_FAIL,
            detail_zh="config.yaml 解析失败",
            detail_en="config.yaml failed to parse",
        )
    if not isinstance(data, dict):
        return CheckResult(
            key="mcp_tools",
            name_zh="MCP / 工具配置能否加载",
            name_en="MCP / tools config loads",
            status=STATUS_FAIL,
            detail_zh="config.yaml 顶层结构异常",
            detail_en="unexpected config.yaml structure",
        )
    sections = [key for key in ("mcp", "mcp_servers", "tools", "toolsets") if key in data]
    if not sections:
        return CheckResult(
            key="mcp_tools",
            name_zh="MCP / 工具配置能否加载",
            name_en="MCP / tools config loads",
            status=STATUS_PASS,
            detail_zh="未配置 MCP/tools 段落（默认工具集）",
            detail_en="no MCP/tools sections configured (default toolsets)",
        )

    mcp_section = data.get("mcp") or data.get("mcp_servers")
    server_count = len(mcp_section) if isinstance(mcp_section, dict) else None
    if env.hermes_cli and server_count:
        result = run_process([env.hermes_cli, "mcp", "list"], timeout=timeout)
        if result.timed_out:
            return CheckResult(
                key="mcp_tools",
                name_zh="MCP / 工具配置能否加载",
                name_en="MCP / tools config loads",
                status=STATUS_WARN,
                detail_zh="`hermes mcp list` 超时",
                detail_en="`hermes mcp list` timed out",
            )
        if not result.ok:
            return CheckResult(
                key="mcp_tools",
                name_zh="MCP / 工具配置能否加载",
                name_en="MCP / tools config loads",
                status=STATUS_WARN,
                detail_zh=f"`hermes mcp list` exit={result.returncode}: {result.first_lines[:200]}",
                detail_en=f"`hermes mcp list` exit={result.returncode}: {result.first_lines[:200]}",
            )
    detail = f"sections: {', '.join(sections)}"
    if server_count is not None:
        detail += f"; mcp servers: {server_count}"
    return CheckResult(
        key="mcp_tools",
        name_zh="MCP / 工具配置能否加载",
        name_en="MCP / tools config loads",
        status=STATUS_PASS,
        detail_zh=detail,
        detail_en=detail,
    )


def _check_smoke(env: LocalEnv, *, timeout: float) -> CheckResult:
    """Optional end-to-end smoke test - costs an LLM call, therefore opt-in."""
    if not env.hermes_cli:
        return CheckResult(
            key="smoke",
            name_zh="Smoke test（一次真实对话）",
            name_en="Smoke test (one real chat turn)",
            status=STATUS_SKIP,
            detail_zh="hermes 不可用",
            detail_en="hermes unavailable",
        )
    result = run_process(
        [env.hermes_cli, "chat", "-q", "Reply with the single word: OK"],
        timeout=timeout,
    )
    if result.ok and "OK" in result.output.upper():
        return CheckResult(
            key="smoke",
            name_zh="Smoke test（一次真实对话）",
            name_en="Smoke test (one real chat turn)",
            status=STATUS_PASS,
            detail_zh="模型调用成功",
            detail_en="model call succeeded",
        )
    return CheckResult(
        key="smoke",
        name_zh="Smoke test（一次真实对话）",
        name_en="Smoke test (one real chat turn)",
        status=STATUS_FAIL if not result.ok else STATUS_WARN,
        detail_zh=f"exit={result.returncode}: {result.first_lines[:200]}",
        detail_en=f"exit={result.returncode}: {result.first_lines[:200]}",
    )
