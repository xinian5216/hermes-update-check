"""Update execution: snapshot -> update -> health check -> (rollback).

Nothing in here runs without an explicit confirmation from the caller: this
module never decides *by itself* that an update should happen. The CLI owns
the y/N prompt, this module owns the mechanics.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .config import Config, resolve_state_dir
from .errors import AbortedError, CommandError
from .health import HealthReport, run_health_checks
from .local_env import LocalEnv, detect_local_env
from .logging_setup import get_logger
from .overrides import (
    ApplyResult,
    OverrideReport,
    apply_overrides,
    classify,
    load_registry,
    patches_dir,
    restore_clean_state,
)
from .state import (
    STAGE_CLEANED,
    STAGE_COMMITTED,
    STAGE_OVERRIDES_REAPPLIED,
    STAGE_PREPARED,
    STAGE_UPDATED,
    STAGE_VERIFIED,
    STATUS_FAILED,
    STATUS_HEALTH_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_ROLLED_BACK,
    STATUS_SUCCEEDED,
    StateStore,
    UpdateState,
)
from .util import ProcResult, ensure_dir, run_process, run_streaming, sha256_file, utcnow, write_json

LineCallback = Optional[Callable[[str], None]]

#: Fingerprinted before every update (copied only when it is safe to copy).
SNAPSHOT_TARGETS: tuple[tuple[str, str, bool], ...] = (
    ("config.yaml", "config.yaml", True),  # settings - copied
    (".env", ".env", False),  # secrets - hash only, never copied
    ("auth.json", "auth.json", False),  # credentials - hash only
    ("state.db", "state.db", False),  # session store - hash/metadata only (can be huge)
    ("cron", "cron", False),
    ("skills", "skills", False),
    ("memories", "memories", False),
    ("desktop-plugins", "desktop-plugins", False),
)

SNAPSHOT_MAX_HASH_BYTES = 256 * 1024 * 1024  # do not hash enormous DBs


@dataclass
class SnapshotEntry:
    name: str
    path: str
    exists: bool
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    copied_to: Optional[str] = None
    note: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "copied_to": self.copied_to,
            "note": self.note,
        }


@dataclass
class Snapshot:
    root: Path
    created_at: str
    entries: list[SnapshotEntry] = field(default_factory=list)
    db_backup: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "created_at": self.created_at,
            "db_backup": self.db_backup,
            "entries": [e.to_dict() for e in self.entries],
        }


@dataclass
class UpdateOutcome:
    ok: bool
    state: UpdateState
    env_before: LocalEnv
    env_after: Optional[LocalEnv] = None
    result: Optional[ProcResult] = None
    health: Optional[HealthReport] = None
    snapshot: Optional[Snapshot] = None
    dry_run: bool = False
    rolled_back: bool = False
    overrides: Optional[OverrideReport] = None
    overrides_apply: Optional[ApplyResult] = None
    overrides_blocked: bool = False
    message_zh: str = ""
    message_en: str = ""

    @property
    def overrides_conflict(self) -> bool:
        """The update itself finished, but the local overrides could not be reapplied."""
        return bool(self.overrides_apply and self.overrides_apply.failed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "rolled_back": self.rolled_back,
            "overrides_blocked": self.overrides_blocked,
            "overrides": self.overrides.to_dict() if self.overrides else None,
            "overrides_apply": self.overrides_apply.to_dict() if self.overrides_apply else None,
            "overrides_conflict": self.overrides_conflict,
            "transaction_stage": self.state.stage,
            "stage_history": list(self.state.stage_history),
            "interrupted": self.state.interrupted,
            "state": self.state.to_dict(),
            "command": self.result.cmd if self.result else None,
            "returncode": self.result.returncode if self.result else None,
            "env_after": self.env_after.to_dict() if self.env_after else None,
            "health": self.health.to_dict() if self.health else None,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
            "message_zh": self.message_zh,
            "message_en": self.message_en,
        }


@dataclass
class RollbackOutcome:
    ok: bool
    steps: list[str] = field(default_factory=list)
    health: Optional[HealthReport] = None
    env_after: Optional[LocalEnv] = None
    dry_run: bool = False
    message_zh: str = ""
    message_en: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "steps": self.steps,
            "health": self.health.to_dict() if self.health else None,
            "env_after": self.env_after.to_dict() if self.env_after else None,
            "message_zh": self.message_zh,
            "message_en": self.message_en,
        }


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #


def create_snapshot(env: LocalEnv, state_root: Path, *, logger: Optional[logging.Logger] = None) -> Snapshot:
    """Fingerprint (and partially copy) the Hermes home before an update.

    The point is *verifiability*: after a bad update we can prove whether
    config.yaml changed, and we have a copy of it to restore. The heavy
    artefacts (state.db) are backed up by ``hermes update --backup`` itself.
    """
    log = logger or get_logger("snapshot")
    store = StateStore(state_root)
    store.ensure()
    root = store.snapshot_dir_for()
    root.mkdir(parents=True, exist_ok=True)
    snapshot = Snapshot(root=root, created_at=utcnow().isoformat())

    for name, rel, copy_it in SNAPSHOT_TARGETS:
        path = env.hermes_home / rel
        entry = SnapshotEntry(name=name, path=str(path), exists=path.exists())
        if path.exists():
            try:
                if path.is_dir():
                    entry.note = "directory (metadata only)"
                    entry.size_bytes = _dir_bytes(path)
                else:
                    entry.size_bytes = path.stat().st_size
                    if entry.size_bytes <= SNAPSHOT_MAX_HASH_BYTES:
                        entry.sha256 = sha256_file(path)
                    else:
                        entry.note = "too large to hash"
                    if copy_it:
                        target = root / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, target)
                        entry.copied_to = str(target)
                        entry.note = "copied"
            except OSError as exc:
                entry.note = f"failed: {exc}"
                log.warning("snapshot entry %s failed: %s", name, exc)
        snapshot.entries.append(entry)

    # A consistent copy of state.db while Hermes may be writing to it.
    snapshot.db_backup = _backup_state_db(env.state_db, root, logger=log)
    write_json(root / "snapshot.json", snapshot.to_dict())
    log.info("snapshot written to %s", root)
    return snapshot


def _dir_bytes(path: Path) -> Optional[int]:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError:
        return None
    return total


def _backup_state_db(state_db: Path, root: Path, *, logger: logging.Logger) -> Optional[str]:
    """Use SQLite's online backup API, which is safe while Hermes is running."""
    if not state_db.exists():
        return None
    target = root / "state.db.snapshot"
    try:
        with sqlite3.connect(str(state_db), timeout=10) as source, sqlite3.connect(str(target)) as dest:
            source.backup(dest)
        return str(target)
    except sqlite3.Error as exc:
        logger.warning("state.db snapshot failed: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #


def build_update_command(
    cfg: Config,
    env: LocalEnv,
    *,
    backup: bool,
    yes: bool,
    branch: Optional[str] = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Compose the ``hermes update`` invocation (nothing is executed here)."""
    if not env.hermes_cli:
        raise CommandError("hermes executable not found; cannot update")
    cmd: list[str] = [env.hermes_cli, "update"]
    cmd.append("--backup" if backup else "--no-backup")
    if yes:
        cmd.append("--yes")
    if branch and env.is_git_install:
        cmd.extend(["--branch", branch])
    cmd.extend(str(a) for a in extra_args)
    return cmd


def run_update(
    cfg: Config,
    env: LocalEnv,
    *,
    state_root: Optional[Path] = None,
    backup: bool = True,
    yes: bool = False,
    branch: Optional[str] = None,
    extra_args: Sequence[str] = (),
    dry_run: bool = False,
    skip_health_check: bool = False,
    auto_rollback: bool = False,
    on_line: LineCallback = None,
    logger: Optional[logging.Logger] = None,
    timeout: float = 1800.0,
) -> UpdateOutcome:
    """Execute the update. The caller has already obtained confirmation."""
    log = logger or get_logger("update")
    store = StateStore(state_root or resolve_state_dir(cfg))
    store.ensure()

    state = UpdateState(
        previous_version=env.version,
        previous_tag=env.release_tag,
        previous_commit=env.git.full_commit if env.git else None,
        previous_branch=env.git.branch if env.git else None,
        previous_dirty=bool(env.git and env.git.dirty),
        install_kind=env.install_kind,
        install_dir=str(env.install_dir) if env.install_dir else None,
        hermes_home=str(env.hermes_home),
        venv_python=str(env.venv_python) if env.venv_python else None,
        python_version=env.python_version,
        uv_path=env.uv_path,
        update_time=utcnow().isoformat(),
        command=None,
        status=STATUS_IN_PROGRESS,
    )

    # -- dry run: show the plan, change nothing ---------------------------- #
    if dry_run:
        plan_cmd = [env.hermes_cli or "hermes", "update", "--plan"]
        result = run_process(plan_cmd, timeout=min(timeout, 120.0))
        return UpdateOutcome(
            ok=result.ok,
            state=state,
            env_before=env,
            result=result,
            dry_run=True,
            message_zh="dry-run：未做任何修改",
            message_en="dry-run: nothing was changed",
        )

    # -- phase 4: managed local overrides ---------------------------------- #
    # Unknown dirty aborts before anything is touched; registered overrides are
    # snapshotted, temporarily removed, and reapplied after the update.
    overrides_report: Optional[OverrideReport] = None
    if env.install_dir is not None:
        registry = load_registry(store.root)
        if not registry.empty:
            overrides_report = classify(env.install_dir, registry)
            state.overrides_before = [entry.path for entry in registry.files]
            state.overrides_patch_file = next((entry.patch_file for entry in registry.files if entry.patch_file), None)
            state.overrides_patch_sha256 = next(
                (entry.patch_sha256 for entry in registry.files if entry.patch_sha256), None
            )
            if overrides_report.unknown and cfg.local_overrides.block_unknown_changes:
                state.advance(STAGE_PREPARED, note="aborted: unregistered changes")
                state.mark(STATUS_FAILED, note=f"{overrides_report.unknown_count} unregistered change(s)")
                store.save_update_state(state)
                return UpdateOutcome(
                    ok=False,
                    state=state,
                    env_before=env,
                    overrides=overrides_report,
                    overrides_blocked=True,
                    message_zh=(
                        f"中止：工作区有 {overrides_report.unknown_count} 个未登记的修改。"
                        "先登记为本地定制（huc overrides register）或提交/暂存它们。"
                    ),
                    message_en=(
                        f"aborted: {overrides_report.unknown_count} unregistered change(s). "
                        "Register them (`huc overrides register`) or commit/stash them first."
                    ),
                )
            if overrides_report.drifted and cfg.local_overrides.block_drifted_overrides:
                state.advance(STAGE_PREPARED, note="aborted: drifted overrides")
                state.mark(STATUS_FAILED, note="drifted local overrides")
                store.save_update_state(state)
                return UpdateOutcome(
                    ok=False,
                    state=state,
                    env_before=env,
                    overrides=overrides_report,
                    overrides_blocked=True,
                    message_zh=(
                        f"中止：{overrides_report.drifted_count} 个本地定制在登记之后又被改过（drift）。"
                        "先运行 huc overrides refresh 或确认改动。"
                    ),
                    message_en=(
                        f"aborted: {overrides_report.drifted_count} override(s) drifted since registration. "
                        "Run `huc overrides refresh` or review them first."
                    ),
                )

    # -- snapshot + state before touching anything ------------------------- #
    snapshot = create_snapshot(env, store.root, logger=log)
    state.snapshot_path = str(snapshot.root)
    cmd = build_update_command(cfg, env, backup=backup, yes=yes, branch=branch, extra_args=extra_args)
    state.command = " ".join(cmd)
    state.target_version = getattr(state, "target_version", None) or None
    state.advance(STAGE_PREPARED, note="snapshot + update command recorded")
    store.save_update_state(state)

    # Override patches were written at registration time; take a fresh copy so a
    # later refresh cannot invalidate what this transaction is about to use.
    if overrides_report is not None and overrides_report.managed_count:
        for entry in overrides_report.registry.files:
            if entry.patch_file and Path(entry.patch_file).is_file():
                stamp = utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
                backup = patches_dir(store.root) / f"{stamp}-preupdate.patch"
                try:
                    ensure_dir(backup.parent)
                    shutil.copy2(entry.patch_file, backup)
                except OSError as exc:  # pragma: no cover - defensive
                    log.warning("could not back up override patch: %s", exc)

    # -- CLEANED: return the registered paths to upstream before updating --- #
    if overrides_report is not None and overrides_report.managed_count:
        if not restore_clean_state(env.install_dir, overrides_report, runner=None):
            state.advance(STAGE_CLEANED, note="could not restore the upstream state")
            state.mark(STATUS_FAILED, note="override restore refused")
            store.save_update_state(state)
            return UpdateOutcome(
                ok=False,
                state=state,
                env_before=env,
                snapshot=snapshot,
                overrides=overrides_report,
                overrides_blocked=True,
                message_zh="中止：无法安全地把已登记的定制暂时还原（有未登记改动或 git 报错），未执行更新。",
                message_en="aborted: the registered overrides could not be safely set aside - nothing was updated.",
            )
        state.advance(STAGE_CLEANED, note="registered overrides set aside")

    log.info("running: %s", " ".join(cmd))
    result = run_streaming(cmd, timeout=timeout, cwd=env.install_dir, on_line=on_line)

    # -- re-detect the environment ----------------------------------------- #
    env_after = detect_local_env(cfg, logger=log, run_upstream_check=False)

    if not result.ok:
        state.mark(STATUS_FAILED, note=f"hermes update exit={result.returncode} {result.error or ''}".strip())
        state.new_version = env_after.version
        state.new_commit = env_after.git.commit if env_after.git else None
        # Section 19: an update that fails must give the user their checkout back.
        restore = None
        if overrides_report is not None and overrides_report.managed_count and env.install_dir is not None:
            restore = apply_overrides(env.install_dir, store.root, overrides_report.registry)
            state.overrides_reapplied = list(restore.applied)
            state.overrides_conflicts = list(restore.conflicts)
            state.advance(STAGE_OVERRIDES_REAPPLIED, note="restored after a failed update")
        store.save_update_state(state)
        message_zh = "更新命令执行失败（未完成），可用 rollback 恢复"
        message_en = "the update command failed; `rollback` is available"
        if restore is not None:
            if restore.ok:
                message_zh += "；本地定制已恢复"
                message_en += "; local overrides were restored"
            else:
                message_zh += "；本地定制恢复失败，patch 已保留在 overrides/patches"
                message_en += "; restoring local overrides FAILED - the patch is kept in overrides/patches"
        return UpdateOutcome(
            ok=False,
            state=state,
            env_before=env,
            env_after=env_after,
            result=result,
            snapshot=snapshot,
            overrides=overrides_report,
            overrides_apply=restore,
            message_zh=message_zh,
            message_en=message_en,
        )

    state.advance(STAGE_UPDATED, note=f"hermes update exit={result.returncode}")
    state.new_version = env_after.version
    state.new_commit = env_after.git.commit if env_after.git else None

    # -- OVERRIDES_REAPPLIED: put the local customization back ------------- #
    apply_result: Optional[ApplyResult] = None
    if overrides_report is not None and overrides_report.managed_count and env.install_dir is not None:
        apply_result = apply_overrides(
            env.install_dir,
            store.root,
            overrides_report.registry,
            strategy=cfg.local_overrides.reapply_strategy,
        )
        state.overrides_reapplied = list(apply_result.applied)
        state.overrides_conflicts = list(apply_result.conflicts)
        state.advance(
            STAGE_OVERRIDES_REAPPLIED, note=f"applied={len(apply_result.applied)} failed={len(apply_result.failed)}"
        )
        store.save_update_state(state)
        if not apply_result.ok:
            # Section 18: the update itself is done, but the customization could
            # not be reapplied. Stop here, keep the patch, and hand over to the user.
            state.mark(
                STATUS_FAILED,
                note="update ok, local overrides could not be reapplied safely",
            )
            store.save_update_state(state)
            return UpdateOutcome(
                ok=False,
                state=state,
                env_before=env,
                env_after=env_after,
                result=result,
                snapshot=snapshot,
                overrides=overrides_report,
                overrides_apply=apply_result,
                message_zh=(
                    "LOCAL OVERRIDE CONFLICT：更新本身已完成，但本地定制无法安全地重新应用。"
                    "patch 与冲突信息已保留在 overrides/ 下，请人工合并（未丢弃任何修改）。"
                ),
                message_en=(
                    "LOCAL OVERRIDE CONFLICT: the update itself completed, but the local overrides could not be "
                    "reapplied safely. The patch and conflict details are kept under overrides/ - resolve manually "
                    "(nothing was discarded)."
                ),
            )

    # -- health check ------------------------------------------------------- #
    health: Optional[HealthReport] = None
    if not skip_health_check and cfg.update.health_check_after_update:
        health = run_health_checks(
            cfg,
            env_after,
            state_root=store.root,
            logger=log,
            gateway_was_running=pre_update_gateway_running(env, logger=log),
        )

    if health is not None and not health.healthy:
        state.status = STATUS_HEALTH_FAILED
        state.health_summary = health.summary_line(lang="en")
        store.save_update_state(state)
        outcome = UpdateOutcome(
            ok=False,
            state=state,
            env_before=env,
            env_after=env_after,
            result=result,
            health=health,
            snapshot=snapshot,
            message_zh="更新完成但健康检查未通过（UPDATE FAILED HEALTH CHECK）",
            message_en="update finished but the health check failed (UPDATE FAILED HEALTH CHECK)",
        )
        if auto_rollback:
            rollback = run_rollback(
                cfg,
                env_after,
                store=store,
                state=state,
                yes=True,
                reinstall_deps=True,
                logger=log,
                on_line=on_line,
            )
            outcome.rolled_back = rollback.ok
            outcome.message_zh += "；已自动回滚" if rollback.ok else "；自动回滚失败，请手动处理"
            outcome.message_en += (
                "; auto-rollback done" if rollback.ok else "; auto-rollback FAILED - manual action needed"
            )
        return outcome

    state.advance(STAGE_VERIFIED, note=state.health_summary or "no health check configured")
    state.status = STATUS_SUCCEEDED
    state.advance(STAGE_COMMITTED, note="transaction finished")
    if health is not None:
        state.health_summary = health.summary_line(lang="en")
    store.save_update_state(state)

    if cfg.update.restart_gateway and env_after.hermes_cli:
        restart = run_process([env_after.hermes_cli, "gateway", "restart"], timeout=180.0)
        state.notes.append(f"gateway restart exit={restart.returncode}")

    return UpdateOutcome(
        ok=True,
        state=state,
        env_before=env,
        env_after=env_after,
        result=result,
        health=health,
        snapshot=snapshot,
        message_zh="更新成功，健康检查通过",
        message_en="update succeeded and the health check passed",
    )


# --------------------------------------------------------------------------- #
# rollback
# --------------------------------------------------------------------------- #


def pre_update_gateway_running(env: LocalEnv, *, logger: Optional[logging.Logger] = None) -> Optional[bool]:
    """Was the gateway running *before* we touched anything?

    Used to make the post-update gateway probe comparative: only a
    running -> not-running transition is a health failure.
    """
    from .local_env import gateway_status, parse_gateway_status

    if not env.hermes_cli:
        return None
    try:
        result = gateway_status(env.hermes_cli, timeout=45.0)
    except Exception as exc:  # pragma: no cover - defensive
        (logger or get_logger("update")).debug("gateway probe failed: %s", exc)
        return None
    status, _detail = parse_gateway_status(result)
    return status == "PASS"


def dependency_install_command(env: LocalEnv) -> list[str]:
    """How to reinstall Hermes' Python dependencies after a git checkout.

    ``uv`` first (that is what the Hermes installer uses), ``pip`` as fallback.
    A bare ``git checkout`` is explicitly not enough - the venv must match the
    checked-out code.
    """
    if env.uv_path and env.venv_python:
        return [env.uv_path, "pip", "install", "-e", ".[all]", "--python", str(env.venv_python)]
    if env.venv_python:
        return [str(env.venv_python), "-m", "pip", "install", "-e", ".[all]"]
    return []


def run_rollback(
    cfg: Config,
    env: LocalEnv,
    *,
    store: Optional[StateStore] = None,
    state: Optional[UpdateState] = None,
    yes: bool = False,
    to_ref: Optional[str] = None,
    reinstall_deps: bool = True,
    restore_backup: Optional[str] = None,
    in_place_restore: bool = False,
    dry_run: bool = False,
    on_line: LineCallback = None,
    logger: Optional[logging.Logger] = None,
    timeout: float = 900.0,
) -> RollbackOutcome:
    """Restore the pre-update version: git ref + dependencies (+ optional backup)."""
    log = logger or get_logger("rollback")
    store = store or StateStore(resolve_state_dir(cfg))
    state = state or store.load_update_state()
    if state is None:
        raise CommandError(
            "no update_state.json found - nothing to roll back to",
            hint="roll back manually with git, or restore a backup from HERMES_HOME/backups",
        )

    target_ref = to_ref or state.previous_commit or state.previous_tag
    steps: list[str] = []
    if not yes:
        raise AbortedError("rollback requires confirmation (pass --yes in non-interactive use)")

    if not target_ref and not restore_backup:
        raise CommandError(
            "the recorded state has no previous commit/tag and no backup was given",
            hint="use --restore-backup <path> to restore a HERMES_HOME backup instead",
        )

    install_dir = Path(state.install_dir) if state.install_dir else env.install_dir
    plan: list[str] = []
    if target_ref and install_dir:
        plan.append(f"git checkout {target_ref} in {install_dir}")
    if reinstall_deps:
        plan.append("reinstall Python dependencies (uv/pip)")
    if restore_backup:
        plan.append(f"restore backup {restore_backup}")

    if dry_run:
        return RollbackOutcome(
            ok=True,
            steps=[f"[dry-run] {step}" for step in plan],
            dry_run=True,
            message_zh="dry-run：未做任何修改",
            message_en="dry-run: nothing was changed",
        )

    # -- git part ----------------------------------------------------------- #
    if target_ref and install_dir:
        if not (install_dir / ".git").exists():
            raise CommandError(
                f"{install_dir} is not a git checkout; cannot checkout {target_ref}",
                hint="restore a HERMES_HOME backup instead (--restore-backup)",
            )
        fetch = run_process(["git", "-C", str(install_dir), "fetch", "--tags", "--quiet"], timeout=180.0)
        steps.append(f"git fetch --tags: exit={fetch.returncode}" + (f" ({fetch.error})" if fetch.error else ""))

        # -- phase 4: the local overrides come along (doc section 43) ------- #
        # A global `git stash` would sweep a user's registered customization out
        # of sight; set the registered paths aside with git restore instead, then
        # reapply them after the checkout.
        registry = load_registry(store.root)
        override_report = None
        if not registry.empty:
            override_report = classify(install_dir, registry)
            if override_report.unknown and cfg.local_overrides.block_unknown_changes:
                raise CommandError(
                    f"rollback refused: {override_report.unknown_count} unregistered change(s) in the working tree",
                    hint="register them (huc overrides register) or commit/stash them first",
                )

        if state.previous_dirty or _is_dirty(install_dir):
            set_aside = False
            if override_report is not None and override_report.managed_count and not override_report.unknown:
                set_aside = restore_clean_state(install_dir, override_report)
                if set_aside:
                    steps.append("registered overrides set aside with git restore (not stashed)")
            if not set_aside:
                stash = run_process(
                    ["git", "-C", str(install_dir), "stash", "push", "-u", "-m", "hermes-update-check rollback"],
                    timeout=120.0,
                )
                steps.append(f"git stash push: exit={stash.returncode}")

        checkout = run_process(["git", "-C", str(install_dir), "checkout", str(target_ref)], timeout=180.0)
        steps.append(f"git checkout {target_ref}: exit={checkout.returncode}")
        if not checkout.ok:
            raise CommandError(
                f"git checkout {target_ref} failed: {checkout.output.strip()[:400]}",
                hint="resolve the conflict manually, then re-run rollback",
            )

        # The patches are the durable record: after checking out the old base they
        # should apply cleanly again, even if the update had removed the changes.
        if not registry.empty and any(entry.patch_file or entry.snapshot_file for entry in registry.files):
            reapplied = apply_overrides(install_dir, store.root, registry)
            state.overrides_reapplied = list(reapplied.applied)
            state.overrides_conflicts = list(reapplied.conflicts)
            steps.append(f"local overrides reapplied: {len(reapplied.applied)} ok, {len(reapplied.failed)} failed")
            if not reapplied.ok:
                steps.append("LOCAL OVERRIDE CONFLICT: resolve manually (patches kept under overrides/)")

    # -- dependency part ---------------------------------------------------- #
    if reinstall_deps and install_dir:
        dep_cmd = dependency_install_command(env) or dependency_install_command(
            LocalEnv(hermes_home=env.hermes_home, venv_python=env.venv_python, uv_path=env.uv_path)
        )
        if dep_cmd:
            install = run_streaming(dep_cmd, timeout=timeout, cwd=install_dir, on_line=on_line)
            steps.append(f"dependencies: {' '.join(dep_cmd)} -> exit={install.returncode}")
            if not install.ok:
                log.warning("dependency reinstall failed: %s", install.output[-500:])
        else:
            steps.append("dependencies: skipped (no uv/pip available) - reinstall manually")

    # -- optional backup restore ------------------------------------------- #
    if restore_backup:
        steps.extend(
            _restore_hermes_home(
                Path(restore_backup), Path(state.hermes_home or env.hermes_home), in_place=in_place_restore, logger=log
            )
        )

    # -- verify ------------------------------------------------------------- #
    env_after = detect_local_env(cfg, logger=log, run_upstream_check=False)
    health = (
        run_health_checks(cfg, env_after, state_root=store.root, logger=log)
        if cfg.update.health_check_after_update
        else None
    )

    state.status = STATUS_ROLLED_BACK
    state.rollback_time = utcnow().isoformat()
    state.notes.append(f"rollback to {target_ref or restore_backup}")
    if health is not None:
        state.health_summary = f"after rollback: {health.summary_line(lang='en')}"
    store.save_update_state(state)

    return RollbackOutcome(
        ok=health.healthy if health is not None else True,
        steps=steps,
        health=health,
        env_after=env_after,
        message_zh=f"已回滚到 {target_ref or restore_backup}",
        message_en=f"rolled back to {target_ref or restore_backup}",
    )


def _is_dirty(install_dir: Path) -> bool:
    result = run_process(["git", "-C", str(install_dir), "status", "--porcelain"], timeout=60.0)
    return bool(result.ok and result.stdout.strip())


def _restore_hermes_home(backup: Path, hermes_home: Path, *, in_place: bool, logger: logging.Logger) -> list[str]:
    """Restore a HERMES_HOME backup archive.

    ``hermes update --backup`` writes a zip; this helper extracts it to a
    staging directory first, and only touches HERMES_HOME when ``in_place`` is
    set - moving the current (broken) home aside instead of deleting it.
    """
    steps: list[str] = []
    if not backup.exists():
        raise CommandError(f"backup not found: {backup}")

    staging = Path(tempfile.mkdtemp(prefix="hermes-restore-"))
    if backup.is_dir():
        shutil.copytree(backup, staging, dirs_exist_ok=True)
        steps.append(f"copied directory backup into staging {staging}")
    elif zipfile.is_zipfile(backup):
        with zipfile.ZipFile(backup) as archive:
            archive.extractall(staging)
        steps.append(f"extracted backup into staging {staging}")
    else:
        raise CommandError(f"unsupported backup format: {backup}")

    if not in_place:
        steps.append(f"staging ready at {staging}; re-run with --in-place to overwrite {hermes_home}")
        return steps

    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    broken = hermes_home.with_name(hermes_home.name + f".broken-{stamp}")
    try:
        if hermes_home.exists():
            hermes_home.rename(broken)
            steps.append(f"moved current HERMES_HOME to {broken}")
        shutil.copytree(staging, hermes_home, dirs_exist_ok=True)
        steps.append(f"restored HERMES_HOME from {backup}")
    except OSError as exc:
        raise CommandError(
            f"restore failed: {exc}", hint=f"previous home is at {broken}; staging at {staging}"
        ) from exc
    logger.info("HERMES_HOME restored from %s", backup)
    return steps
