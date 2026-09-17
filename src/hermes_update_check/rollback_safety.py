"""Can we actually get back if the update goes wrong?

Phase 3 keeps exactly one local, non-negotiable blocker before *executing* an
update: if rollback is not available, running the update is not allowed - a broken
release is survivable, a broken release with no way back is not.

The probe is deliberately cheap and read-only: it looks at the git checkout, the
recorded ``update_state.json``, disk space, writability and the identifiable
virtualenv. It never runs ``hermes update``, never writes anything and never
deletes anything.

``UNKNOWN`` is reported honestly (and never treated as ``PASS``): when there is
nothing to judge - no git checkout, no state file yet - the verdict says so.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .config import Config, resolve_state_dir
from .local_env import LocalEnv
from .logging_setup import get_logger
from .provenance import CodeProvenance
from .state import StateStore, UpdateState
from .util import disk_free_bytes, run_process, wrap_command

SAFETY_PASS = "PASS"
SAFETY_WARN = "WARN"
SAFETY_FAIL = "FAIL"
SAFETY_UNKNOWN = "UNKNOWN"

_RANK = {SAFETY_PASS: 0, SAFETY_UNKNOWN: 1, SAFETY_WARN: 2, SAFETY_FAIL: 3}


@dataclass
class SafetyCheck:
    key: str
    zh: str
    en: str
    status: str
    detail_zh: str = ""
    detail_en: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label_zh": self.zh,
            "label_en": self.en,
            "status": self.status,
            "detail_zh": self.detail_zh,
            "detail_en": self.detail_en,
        }


@dataclass
class RollbackSafety:
    status: str = SAFETY_UNKNOWN
    checks: list[SafetyCheck] = field(default_factory=list)
    reasons_zh: list[str] = field(default_factory=list)
    reasons_en: list[str] = field(default_factory=list)
    previous_ref: str = ""
    install_dir: str = ""
    state_file: str = ""

    @property
    def failed(self) -> bool:
        return self.status == SAFETY_FAIL

    @property
    def unsafe(self) -> bool:
        """FAIL or UNKNOWN - anything that is not a clear pass."""
        return self.status in {SAFETY_FAIL, SAFETY_UNKNOWN}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "previous_ref": self.previous_ref,
            "install_dir": self.install_dir,
            "state_file": self.state_file,
            "checks": [check.to_dict() for check in self.checks],
            "reasons_zh": self.reasons_zh,
            "reasons_en": self.reasons_en,
        }


def _default_git_probe(install_dir: Path) -> Callable[[str], bool]:
    def probe(commit: str) -> bool:
        if not commit or not install_dir or not Path(install_dir).exists():
            return False
        result = run_process(
            wrap_command(["git", "-C", str(install_dir), "cat-file", "-e", f"{commit}^{{commit}}"]),
            timeout=20.0,
        )
        return result.returncode == 0

    return probe


def assess_rollback_safety(
    cfg: Config,
    env: LocalEnv,
    provenance: CodeProvenance,
    *,
    state: Optional[UpdateState] = None,
    state_dir: Optional[Path] = None,
    git_probe: Optional[Callable[[str], bool]] = None,
    disk_free_gib: Optional[float] = None,
    logger: Optional[logging.Logger] = None,
) -> RollbackSafety:
    """Read-only assessment of whether `rollback` would work right now."""
    log = logger or get_logger("safety")
    raw_install = getattr(env, "install_dir", None)
    install_dir = Path(raw_install) if raw_install else Path()
    state_dir = Path(state_dir) if state_dir is not None else resolve_state_dir(cfg)
    state_file = state_dir / "update_state.json"

    safety = RollbackSafety(install_dir=str(install_dir) if raw_install else "")
    safety.state_file = str(state_file)

    if state is None:
        try:
            state = StateStore(state_dir).load_update_state()
        except Exception as exc:  # pragma: no cover - defensive: never crash the probe
            log.debug("could not load update state: %s", exc)
            state = None

    previous_ref = ""
    if state is not None:
        previous_ref = state.previous_commit or state.previous_tag or ""
    safety.previous_ref = previous_ref

    # 1. git checkout ------------------------------------------------------- #
    if provenance.is_git_install:
        safety.checks.append(
            SafetyCheck(
                key="git_install",
                zh="Hermes 是 Git 安装",
                en="Hermes installed from git",
                status=SAFETY_PASS,
                detail_zh=f"安装目录：{install_dir}",
                detail_en=f"install dir: {install_dir}",
            )
        )
    else:
        safety.checks.append(
            SafetyCheck(
                key="git_install",
                zh="Hermes 是 Git 安装",
                en="Hermes installed from git",
                status=SAFETY_WARN,
                detail_zh="不是 Git 安装（pip/uv/docker）：回滚需要重装旧版本，本工具只能给出提示",
                detail_en="not a git checkout (pip/uv/docker): rolling back needs a reinstall; no automatic undo",
            )
        )

    # 2. recorded previous version ------------------------------------------ #
    if previous_ref:
        safety.checks.append(
            SafetyCheck(
                key="previous_state",
                zh="已记录可回退版本",
                en="previous version recorded",
                status=SAFETY_PASS,
                detail_zh=f"update_state.json 记录：{previous_ref}",
                detail_en=f"update_state.json records: {previous_ref}",
            )
        )
    else:
        safety.checks.append(
            SafetyCheck(
                key="previous_state",
                zh="已记录可回退版本",
                en="previous version recorded",
                status=SAFETY_WARN,
                detail_zh="尚无 update_state.json（首次更新会在更新前创建）",
                detail_en="no update_state.json yet (the first update creates one before touching anything)",
            )
        )

    # 3. commit still reachable locally ------------------------------------- #
    probe = git_probe if git_probe is not None else _default_git_probe(install_dir)
    if not previous_ref:
        safety.checks.append(
            SafetyCheck(
                key="commit_recoverable",
                zh="回退目标可用",
                en="rollback target reachable",
                status=SAFETY_UNKNOWN,
                detail_zh="没有记录的回退目标，无法判断",
                detail_en="no recorded rollback target, cannot judge",
            )
        )
    elif not provenance.is_git_install:
        safety.checks.append(
            SafetyCheck(
                key="commit_recoverable",
                zh="回退目标可用",
                en="rollback target reachable",
                status=SAFETY_UNKNOWN,
                detail_zh="非 Git 安装，无法用 git 校验回退目标",
                detail_en="not a git checkout, the rollback target cannot be verified with git",
            )
        )
    else:
        try:
            reachable = bool(probe(previous_ref))
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("git probe failed: %s", exc)
            reachable = False
        safety.checks.append(
            SafetyCheck(
                key="commit_recoverable",
                zh="回退目标可用",
                en="rollback target reachable",
                status=SAFETY_PASS if reachable else SAFETY_FAIL,
                detail_zh=f"{previous_ref} {'在本地仓库中可找到' if reachable else '在本地仓库中找不到（可能被 GC 或不在这个目录）'}",
                detail_en=(
                    f"{previous_ref} {'is present in the local repository' if reachable else 'is NOT present in the local repository (gc or wrong dir)'}"
                ),
            )
        )

    # 4. state dir writable -------------------------------------------------- #
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        writable = os.access(state_dir, os.W_OK)
    except OSError as exc:
        log.debug("state dir not creatable: %s", exc)
        writable = False
    safety.checks.append(
        SafetyCheck(
            key="state_writable",
            zh="状态目录可写",
            en="state directory writable",
            status=SAFETY_PASS if writable else SAFETY_FAIL,
            detail_zh=f"{state_dir}{'' if writable else '（不可写：无法保存快照与回滚状态）'}",
            detail_en=f"{state_dir}{'' if writable else ' (not writable: snapshots and rollback state cannot be stored)'}",
        )
    )

    # 5. disk space --------------------------------------------------------- #
    if disk_free_gib is None:
        try:
            free_bytes = disk_free_bytes(Path(install_dir) if raw_install else state_dir)
            disk_free_gib = round(free_bytes / (1024**3), 1) if free_bytes is not None else None
        except Exception:  # pragma: no cover - defensive
            disk_free_gib = None
    minimum = float(getattr(cfg.update, "min_free_disk_gib", 2.0))
    if disk_free_gib is None:
        status = SAFETY_UNKNOWN
        detail_zh, detail_en = "无法读取磁盘剩余空间", "free disk space could not be read"
    elif disk_free_gib < minimum:
        status = SAFETY_WARN
        detail_zh = f"剩余 {disk_free_gib:.1f} GiB，低于配置的 {minimum:g} GiB（备份与更新可能失败）"
        detail_en = f"{disk_free_gib:.1f} GiB free, below the configured {minimum:g} GiB (backup/update may fail)"
    else:
        status = SAFETY_PASS
        detail_zh = f"剩余 {disk_free_gib:.1f} GiB（>= {minimum:g} GiB）"
        detail_en = f"{disk_free_gib:.1f} GiB free (>= {minimum:g} GiB)"
    safety.checks.append(
        SafetyCheck(key="disk_space", zh="磁盘空间", en="disk space", status=status, detail_zh=detail_zh, detail_en=detail_en)
    )

    # 6. virtualenv identifiable (dependencies must be reinstallable) -------- #
    venv = Path(env.venv_python) if getattr(env, "venv_python", None) else None
    if venv is None:
        status = SAFETY_WARN
        detail_zh, detail_en = "无法识别 Hermes 的 venv/python（回滚后需手动重装依赖）", (
            "Hermes venv/python not identified (dependencies must be reinstalled by hand after a rollback)"
        )
    elif venv.exists():
        status = SAFETY_PASS
        detail_zh, detail_en = f"{venv}", f"{venv}"
    else:
        status = SAFETY_WARN
        detail_zh = f"记录的 venv 不存在：{venv}"
        detail_en = f"recorded venv does not exist: {venv}"
    safety.checks.append(
        SafetyCheck(
            key="venv_python", zh="Python 环境可识别", en="python environment identifiable", status=status, detail_zh=detail_zh, detail_en=detail_en
        )
    )

    # 7. backup support ------------------------------------------------------ #
    backups_dir = Path(getattr(env, "hermes_home", state_dir)) / "backups"
    if backups_dir.exists():
        status = SAFETY_PASS
        detail_zh, detail_en = f"{backups_dir} 存在", f"{backups_dir} exists"
    elif not getattr(env, "hermes_cli", None):
        status = SAFETY_WARN
        detail_zh, detail_en = "未找到 hermes 可执行文件，无法确认备份支持", "hermes executable not found; backup support unverified"
    else:
        status = SAFETY_UNKNOWN
        detail_zh = f"{backups_dir} 不存在（首次备份会创建）"
        detail_en = f"{backups_dir} does not exist yet (the first backup creates it)"
    safety.checks.append(
        SafetyCheck(
            key="backup_support", zh="备份支持", en="backup support", status=status, detail_zh=detail_zh, detail_en=detail_en
        )
    )

    # -- aggregate ----------------------------------------------------------- #
    worst = max((check.status for check in safety.checks), key=lambda status: _RANK.get(status, 1), default=SAFETY_UNKNOWN)
    if worst == SAFETY_FAIL:
        safety.status = SAFETY_FAIL
    elif any(check.status == SAFETY_WARN for check in safety.checks):
        safety.status = SAFETY_WARN
    elif all(check.status == SAFETY_PASS for check in safety.checks):
        safety.status = SAFETY_PASS
    else:
        safety.status = SAFETY_UNKNOWN

    for check in safety.checks:
        if check.status in {SAFETY_FAIL, SAFETY_WARN}:
            safety.reasons_zh.append(f"{check.zh}：{check.detail_zh}")
            safety.reasons_en.append(f"{check.en}: {check.detail_en}")
    if safety.status == SAFETY_PASS:
        safety.reasons_zh.append("回滚路径完整：Git 安装、回退目标可用、状态目录可写、磁盘充足")
        safety.reasons_en.append(
            "rollback path intact: git checkout, reachable rollback target, writable state dir, enough disk"
        )
    return safety
