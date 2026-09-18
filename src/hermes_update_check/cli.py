"""Command line interface.

Safety contract:

* ``check`` / ``report`` / ``watch`` never modify anything;
* ``update`` asks for an explicit ``y`` (default ``N``) unless ``--yes`` is
  given or the user deliberately set ``auto_update: true``;
* ``rollback`` restores the recorded pre-update version;
* every command returns a documented exit code (see ``errors.py``).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from . import TOOL_NAME, __version__
from .advisor import (
    RECOMMEND_AHEAD_OF_STABLE,
    RECOMMEND_MANUAL_REVIEW,
)
from .checker import UpdateCheck, run_check
from .clusters import SEVERITY_CRITICAL
from .config import (
    Config,
    default_config_path,
    load_config,
    resolve_hermes_home,
    resolve_state_dir,
    write_default_config,
)
from .console import Console
from .errors import (
    EXIT_ABORTED,
    EXIT_CONFIG,
    EXIT_ERROR,
    EXIT_HEALTH_FAILED,
    EXIT_INSUFFICIENT_DATA,
    EXIT_OK,
    EXIT_PREFLIGHT_FAILED,
    EXIT_USAGE,
    EXIT_WAIT,
    ConfigError,
    HermesUpdateCheckError,
)
from .health import HealthReport, run_health_checks
from .local_env import LocalEnv, detect_local_env
from .logging_setup import setup_logging
from .notify import NotificationMessage, build_notifiers, describe_notifiers, notify_all
from .preflight import STATUS_FAIL, PreflightReport, run_preflight
from .report import Reporter
from .advisor import (
    RECOMMEND_ACCEPTABLE,
    RECOMMEND_BLOCKED,
    RECOMMEND_MANUAL_REVIEW,
    RECOMMEND_SAFE,
    RECOMMEND_WAIT,
)
from .risk import (
    RECOMMEND_UNKNOWN,
    RECOMMEND_UP_TO_DATE,
)
from .state import StateStore
from .updater import RollbackOutcome, UpdateOutcome, run_rollback, run_update
from .usage_profile import detect_usage_profile, resolve_profile
from .util import humanize_hours

# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description=(
            "Hermes Update Checker - 更新前先做风险评估，绝不自动更新。\n"
            "Hermes Update Checker - assess update risk before you touch anything. Never auto-updates."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes: 0 ok | 10 wait/avoid | 11 insufficient data | 12 health check failed\n"
            "            13 aborted | 14 preflight failed | 1 error | 2 usage | 3 config"
        ),
    )
    parser.add_argument("--version", action="version", version=f"{TOOL_NAME} {__version__}")
    parser.add_argument(
        "--config", metavar="PATH", help="path to config.yaml (default: ~/.config/hermes-update-check/config.yaml)"
    )
    parser.add_argument("--lang", choices=["zh", "en"], help="report language override")
    parser.add_argument("--plain", action="store_true", help="plain text output (no rich formatting)")
    parser.add_argument("--no-color", action="store_true", help="disable colour")
    parser.add_argument("--json", action="store_true", help="machine readable JSON on stdout")
    parser.add_argument("--no-cache", action="store_true", help="ignore the GitHub response cache")
    parser.add_argument("--timeout", type=float, metavar="SECONDS", help="network timeout override")
    parser.add_argument("--quiet", "-q", action="store_true", help="only errors and the final status line")
    parser.add_argument("--verbose", "-v", action="count", default=0, help="-v INFO logs to stderr, -vv DEBUG")

    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="check for updates and print the short risk verdict")
    check.add_argument("--no-issues", action="store_true", help="skip GitHub issue analysis")
    check.add_argument("--offline", action="store_true", help="use cached GitHub data only")

    report = sub.add_parser("report", help="full report (factors, issues, recommendation)")
    report.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    report.add_argument("--output", metavar="FILE", help="write the report to a file")
    report.add_argument("--no-issues", action="store_true", help="skip GitHub issue analysis")

    watch = sub.add_parser("watch", help="cron-friendly check: notify only when something changed")
    watch.add_argument("--notify", action="store_true", help="force sending notifications")
    watch.add_argument("--no-notify", action="store_true", help="never send notifications, just report")
    watch.add_argument("--dry-run", action="store_true", help="show what would be notified")

    upd = sub.add_parser("update", help="update Hermes after risk gate + preflight + confirmation")
    upd.add_argument("--yes", "-y", action="store_true", help="skip the interactive confirmation")
    upd.add_argument(
        "--dry-run", "--plan", dest="dry_run", action="store_true", help="show the update plan, change nothing"
    )
    upd.add_argument(
        "--backup", dest="backup", action="store_true", default=None, help="force a full pre-update backup"
    )
    upd.add_argument(
        "--no-backup", dest="backup", action="store_false", help="skip the pre-update backup (not recommended)"
    )
    upd.add_argument("--branch", metavar="NAME", help="update against this branch (git installs)")
    upd.add_argument("--force", action="store_true", help="proceed even when the risk gate says WAIT/AVOID")
    upd.add_argument(
        "--skip-check", action="store_true", help="skip the online risk check (preflight + confirmation only)"
    )
    upd.add_argument("--skip-health-check", action="store_true", help="do not run the post-update health check")
    upd.add_argument("--auto-rollback", action="store_true", help="roll back automatically when the health check fails")
    upd.add_argument("--restart-gateway", action="store_true", help="restart the gateway after a successful update")

    roll = sub.add_parser("rollback", help="restore the version recorded in update_state.json")
    roll.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    roll.add_argument("--to", metavar="REF", help="git ref to restore (default: recorded commit)")
    roll.add_argument("--no-deps", action="store_true", help="do not reinstall Python dependencies")
    roll.add_argument("--restore-backup", metavar="PATH", help="also restore a HERMES_HOME backup (zip or directory)")
    roll.add_argument(
        "--in-place", action="store_true", help="overwrite HERMES_HOME from the backup (moves the old one aside)"
    )
    roll.add_argument("--dry-run", action="store_true", help="show the rollback plan, change nothing")

    health = sub.add_parser("health", help="run the post-update health checks")
    health.add_argument("--smoke", action="store_true", help="also run a real chat smoke test (costs one LLM call)")
    health.add_argument("--timeout", type=float, default=90.0, metavar="SECONDS")

    pre = sub.add_parser("preflight", help="run the pre-update checks only")
    pre.add_argument(
        "--no-processes", action="store_true", help="skip the process/gateway scan (faster, e.g. in restricted shells)"
    )

    cfg_cmd = sub.add_parser("config", help="show / locate / create the configuration")
    cfg_cmd.add_argument("action", choices=["show", "path", "init"], nargs="?", default="show")
    cfg_cmd.add_argument("--force", action="store_true", help="overwrite an existing config file (config init)")

    profile = sub.add_parser("profile", help="usage profile: what *you* depend on (phase 3)")
    profile.add_argument(
        "action", choices=["show", "detect", "edit"], nargs="?", default="show",
        help="show the profile in use / detect one from your Hermes install / print the YAML to edit",
    )
    profile.add_argument("--write", action="store_true", help="write the result into the config file (a .bak is kept)")
    profile.add_argument("--json", dest="profile_json", action="store_true", help="machine readable output")

    notify_test = sub.add_parser("notify-test", help="send a test notification to every configured channel")
    notify_test.add_argument("--message", default="hermes-update-check test notification")

    sub.add_parser("version", help="print the tool version")
    return parser


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def make_output_encoding_safe() -> None:
    """Never crash on a console that cannot represent the text we print.

    Windows consoles default to a legacy code page (cp1252, cp936, ...), where
    printing Chinese help text or a Chinese report raises ``UnicodeEncodeError``
    and kills the process. Prefer UTF-8 (that is what CI logs, pipes and editors
    expect), and fall back to ``errors="replace"`` so the worst case is a few
    replacement characters instead of a traceback. Found by the Windows CI job.
    """
    if sys.platform == "win32":
        try:  # make the console itself UTF-8 so the characters actually render
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # pragma: no cover - console-less/odd environments
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - not a TextIOWrapper
            try:
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            except Exception:
                pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    make_output_encoding_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "check"

    console = Console(plain=args.plain or False, no_color=args.no_color or False, quiet=args.quiet)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        console.error(str(exc))
        return EXIT_CONFIG

    if args.lang:
        cfg.language = args.lang
    if args.plain:
        cfg.plain_output = True
    if args.timeout:
        cfg.network.timeout_seconds = args.timeout

    state_root = resolve_state_dir(cfg)
    store = StateStore(state_root)
    store.ensure()

    level = "DEBUG" if args.verbose >= 2 else ("INFO" if args.verbose else cfg.logging.level)
    log_file = cfg.logging.file or store.log_file
    logger = setup_logging(level=level, log_file=log_file, console=cfg.logging.console or args.verbose >= 1)

    if not args.json and cfg.warnings and command not in {"version"}:
        for warning in cfg.warnings:
            console.warn(warning)

    try:
        return _dispatch(command, args, cfg, console, store, logger)
    except HermesUpdateCheckError as exc:
        logger.error("%s", exc)
        console.error(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        console.error("interrupted")
        return EXIT_ABORTED
    except Exception as exc:  # pragma: no cover - last resort, keep cron quiet-ish
        logger.exception("unexpected failure")
        console.error(f"unexpected error: {type(exc).__name__}: {exc}")
        return EXIT_ERROR


def _dispatch(
    command: str,
    args: argparse.Namespace,
    cfg: Config,
    console: Console,
    store: StateStore,
    logger: Any,
) -> int:
    if command == "check":
        return cmd_check(args, cfg, console, store, logger)
    if command == "report":
        return cmd_report(args, cfg, console, store, logger)
    if command == "watch":
        return cmd_watch(args, cfg, console, store, logger)
    if command == "update":
        return cmd_update(args, cfg, console, store, logger)
    if command == "rollback":
        return cmd_rollback(args, cfg, console, store, logger)
    if command == "health":
        return cmd_health(args, cfg, console, store, logger)
    if command == "preflight":
        return cmd_preflight(args, cfg, console, store, logger)
    if command == "profile":
        return cmd_profile(args, cfg, console)
    if command == "config":
        return cmd_config(args, cfg, console)
    if command == "notify-test":
        return cmd_notify_test(args, cfg, console, logger)
    if command == "version":
        print(f"{TOOL_NAME} {__version__}")
        return EXIT_OK
    console.error(f"unknown command: {command}")
    return EXIT_USAGE


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_check(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int:
    check = run_check(
        cfg,
        state_root=store.root,
        no_cache=getattr(args, "offline", False),
        include_issues=False if getattr(args, "no_issues", False) else None,
        logger=logger,
    )
    if args.json:
        print(json.dumps(check.to_dict(), indent=2, ensure_ascii=False))
    else:
        Reporter(console, lang=cfg.language).render(check, detailed=False)
    return _check_exit_code(check)


def cmd_report(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int:
    check = run_check(
        cfg,
        state_root=store.root,
        include_issues=False if getattr(args, "no_issues", False) else None,
        logger=logger,
    )
    reporter = Reporter(console, lang=cfg.language)

    if args.format == "json":
        payload = json.dumps(check.to_dict(), indent=2, ensure_ascii=False)
        _emit(payload, args.output, console)
        return _check_exit_code(check)
    if args.format == "markdown":
        text = reporter.to_markdown(check)
        _emit(text, args.output, console)
        return _check_exit_code(check)

    reporter.render(check, detailed=True)
    if args.output:
        path = Path(args.output).expanduser()
        path.write_text(reporter.to_markdown(check), encoding="utf-8")
        console.print(f"\n[{(cfg.language == 'zh' and '报告已写入') or 'report written to'}: {path}]")
    return _check_exit_code(check)


def cmd_watch(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int:
    """Cron entry point: stay silent unless something meaningful changed."""
    watch_state = store.load_watch_state()
    reporter = Reporter(console, lang=cfg.language)
    check = run_check(cfg, state_root=store.root, logger=logger)
    assessment = check.assessment

    current_tag = check.latest.tag if check.latest else None
    current_level = assessment.level if assessment else None
    current_score = assessment.score if assessment else None
    current_recommendation = check.recommended_action

    signal = _watch_signal(cfg, watch_state, check)
    reason_zh = signal[0]
    reason_en = signal[1]
    should_notify = signal[2]

    notify_override = bool(getattr(args, "notify", False))
    no_notify = bool(getattr(args, "no_notify", False))
    if no_notify:
        should_notify = False
    if notify_override:
        should_notify = True

    watch_state.record(
        tag=current_tag,
        level=current_level,
        score=current_score,
        recommendation=current_recommendation,
        channel=check.channel,
        update_status=check.update_status,
        gate_blocks=[g.key for g in (check.gates.blocking if check.gates else [])],
        critical_clusters=[c.key for c in check.clusters if c.severity == SEVERITY_CRITICAL],
        confidence=(assessment.data_confidence if assessment else None),
        critical_features=[f.key for f in (check.readiness.critical_broken if check.readiness else [])],
        personal_action=check.action,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "notify": should_notify,
                    "reason_zh": reason_zh,
                    "reason_en": reason_en,
                    "check": check.to_dict(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        if should_notify:
            console.info(reason_zh if cfg.language == "zh" else reason_en)
            reporter.render(check, detailed=False)
        else:
            state_line = _one_line_status(check, cfg)
            console.print(state_line)
            if args.dry_run:
                console.print(
                    (cfg.language == "zh" and "（dry-run：没有发送通知）") or "(dry-run: no notification sent)"
                )

    if should_notify and not getattr(args, "dry_run", False):
        message = NotificationMessage(
            title=f"Hermes Update {current_level or 'UNKNOWN'}: {current_tag or 'no release'}",
            body=(reason_zh if cfg.language == "zh" else reason_en)
            + "\n\n"
            + "\n".join(
                (assessment.summary_zh if cfg.language == "zh" else assessment.summary_en) if assessment else []
            ),
            level=current_level or "UNKNOWN",
            tag=current_tag,
            url=check.latest.html_url if check.latest else None,
            fields={
                "current": check.env.version_label,
                "latest": current_tag or "?",
                "risk": f"{current_score}/100" if current_score is not None else "UNKNOWN",
                "recommendation": current_recommendation,
            },
        )
        results = notify_all(build_notifiers(cfg), message)
        if results:
            for result in results:
                console.status_line(f"notify/{result.notifier}", "PASS" if result.ok else "WARN", result.detail)
            watch_state.mark_notified(current_tag, reason_zh if cfg.language == "zh" else reason_en)
        else:
            logger.info("watchers: %s", describe_notifiers([]))

    store.save_watch_state(watch_state)
    return _check_exit_code(check)


def cmd_update(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int:
    env = detect_local_env(cfg, logger=logger)

    if args.dry_run:
        return _update_dry_run(args, cfg, console, store, logger, env)

    # 1. risk gate ----------------------------------------------------------- #
    check: Optional[UpdateCheck] = None
    if not args.skip_check:
        check = run_check(cfg, env=env, state_root=store.root, logger=logger)
        Reporter(console, lang=cfg.language).render(check, detailed=False)
        rec = check.recommendation
        if rec is not None and rec.is_blocking and not args.force:
            console.blank()
            blocking_names = ", ".join(rec.blocking_gates) if rec.blocking_gates else rec.decided_by
            console.panel(
                (
                    f"{rec.headline_zh}\n决定依据：{rec.decided_by_label}（{blocking_names}）\n"
                    f"Overall Risk：{rec.overall if rec.overall is not None else 'UNKNOWN'}"
                    f"{'（下界）' if rec.score_is_lower_bound else ''}\n"
                    "如确需升级，请加 --force 重新执行；本次未做任何改动。"
                    if cfg.language == "zh"
                    else f"{rec.headline_en}\nDecided by: {rec.decided_by} ({blocking_names})\n"
                    f"Overall Risk: {rec.overall if rec.overall is not None else 'UNKNOWN'}"
                    f"{' (lower bound)' if rec.score_is_lower_bound else ''}\n"
                    "Re-run with --force if you really want to update; nothing was changed."
                ),
                title="GATE",
                level=rec.action,
            )
            return _check_exit_code(check)
        if check.action in {RECOMMEND_UP_TO_DATE, RECOMMEND_AHEAD_OF_STABLE} and not args.force:
            console.success(
                (
                    "已是最新正式版本，无需更新。"
                    if check.action == RECOMMEND_UP_TO_DATE
                    else "当前代码已领先最新正式版本：没有可执行的更新（如要回到正式 Release 请手动切换）。"
                )
                if cfg.language == "zh"
                else (
                    "Already up to date; nothing to do."
                    if check.action == RECOMMEND_UP_TO_DATE
                    else "Current code is ahead of the latest stable release: there is no update to apply."
                )
            )
            return EXIT_OK
        if (rec is None or not rec.is_blocking) and args.force:
            console.warn("已使用 --force 跳过风险门禁" if cfg.language == "zh" else "--force given: risk gate skipped")

    # 2. preflight ----------------------------------------------------------- #
    console.heading("Preflight" if cfg.language != "zh" else "更新前检查")
    preflight = run_preflight(cfg, env, state_root=store.root, logger=logger)
    _render_preflight(preflight, console, cfg.language)
    if not preflight.ok_to_proceed and not args.force:
        console.blank()
        console.error(
            "更新前检查失败，已中止（可用 --force 强制继续）"
            if cfg.language == "zh"
            else "preflight FAILED; aborting (use --force to override)"
        )
        return EXIT_PREFLIGHT_FAILED

    # 3. confirmation -------------------------------------------------------- #
    backup = cfg.backup_before_update if args.backup is None else bool(args.backup)
    target = (f"v{check.target_version} ({check.target_tag})") if check and check.target_tag else "the newest release"
    if check is not None and check.assessment is not None:
        risk_label = f"{check.assessment.level} ({check.assessment.score_or_unknown})"
    else:
        risk_label = "not checked (--skip-check)"
    if not _confirm_update(args, cfg, console, env, target, backup, risk_label):
        console.print("已取消，未做任何修改。" if cfg.language == "zh" else "Cancelled - nothing was changed.")
        return EXIT_ABORTED

    # 4. update -------------------------------------------------------------- #
    console.heading("Update" if cfg.language != "zh" else "执行更新")
    outcome = run_update(
        cfg,
        env,
        state_root=store.root,
        backup=backup,
        yes=True if args.yes else cfg.auto_update,
        branch=args.branch or (cfg.update.branch if cfg.update.branch else None),
        extra_args=cfg.update.extra_args,
        skip_health_check=args.skip_health_check,
        auto_rollback=args.auto_rollback or cfg.update.auto_rollback_on_failed_health,
        on_line=lambda line: console.print(f"    {line}") if not args.json else None,
        logger=logger,
    )
    if not args.json:
        _render_update_outcome(outcome, console, cfg.language)
    else:
        print(json.dumps(outcome.to_dict(), indent=2, ensure_ascii=False))

    if not outcome.ok:
        if outcome.health is not None and not outcome.health.healthy:
            return EXIT_HEALTH_FAILED
        return EXIT_ERROR
    return EXIT_OK


def _update_dry_run(
    args: argparse.Namespace,
    cfg: Config,
    console: Console,
    store: StateStore,
    logger: Any,
    env: LocalEnv,
) -> int:
    console.heading("Dry run" if cfg.language != "zh" else "演练（不会修改任何东西）")
    check = run_check(cfg, env=env, state_root=store.root, logger=logger)
    Reporter(console, lang=cfg.language).render(check, detailed=False)
    preflight = run_preflight(cfg, env, state_root=store.root, logger=logger)
    _render_preflight(preflight, console, cfg.language)
    outcome = run_update(cfg, env, state_root=store.root, backup=True, dry_run=True, logger=logger)
    console.heading("hermes update --plan")
    console.print(outcome.result.output if outcome.result else "(no output)")
    console.blank()
    console.print(
        "演练结束：以上计划不会被执行。执行真实更新请运行 hermes-update-check update"
        if cfg.language == "zh"
        else "Dry run finished; nothing was executed. Run `hermes-update-check update` to do it for real."
    )
    return _check_exit_code(check)


def cmd_rollback(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int:
    state = store.load_update_state()
    if state is None:
        console.error(
            "没有找到 update_state.json，无法回滚（先运行过一次 update 才会有记录）"
            if cfg.language == "zh"
            else "no update_state.json found - nothing to roll back to"
        )
        # Regression (found in the real E2E): a missing precondition is not an
        # internal error - it must be distinguishable from a crash in cron logs.
        return EXIT_CONFIG

    console.heading("Rollback" if cfg.language != "zh" else "回滚")
    console.kv_table(state.human_summary(lang=cfg.language))

    if args.dry_run:
        outcome = run_rollback(
            cfg,
            detect_local_env(cfg, logger=logger, run_upstream_check=False),
            store=store,
            state=state,
            yes=True,
            to_ref=args.to,
            reinstall_deps=not args.no_deps,
            restore_backup=args.restore_backup,
            in_place_restore=args.in_place,
            dry_run=True,
            logger=logger,
        )
        for step in outcome.steps:
            console.print(f"  - {step}")
        return EXIT_OK

    if not _confirm(
        console,
        cfg.language,
        zh=f"确认回滚到 {args.to or state.previous_commit or state.previous_tag} ？",
        en=f"Roll back to {args.to or state.previous_commit or state.previous_tag}?",
        assume_yes=args.yes,
    ):
        console.print("已取消" if cfg.language == "zh" else "Cancelled")
        return EXIT_ABORTED

    outcome = run_rollback(
        cfg,
        detect_local_env(cfg, logger=logger, run_upstream_check=False),
        store=store,
        state=state,
        yes=True,
        to_ref=args.to,
        reinstall_deps=not args.no_deps,
        restore_backup=args.restore_backup,
        in_place_restore=args.in_place,
        on_line=lambda line: console.print(f"    {line}"),
        logger=logger,
    )
    _render_rollback_outcome(outcome, console, cfg.language)
    return EXIT_OK if outcome.ok else EXIT_HEALTH_FAILED


def cmd_health(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int:
    env = detect_local_env(cfg, logger=logger, run_upstream_check=False)
    report = run_health_checks(
        cfg,
        env,
        state_root=store.root,
        logger=logger,
        smoke_test=bool(args.smoke),
        timeout=float(getattr(args, "timeout", 90.0)),
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        _render_health(report, console, cfg.language)
    return EXIT_OK if report.healthy else EXIT_HEALTH_FAILED


def cmd_preflight(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int:
    env = detect_local_env(cfg, logger=logger, run_upstream_check=False)
    report = run_preflight(cfg, env, state_root=store.root, logger=logger, check_processes=not args.no_processes)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        _render_preflight(report, console, cfg.language)
    return EXIT_OK if report.ok_to_proceed else EXIT_PREFLIGHT_FAILED


def cmd_profile(args: argparse.Namespace, cfg: Config, console: Console) -> int:
    """``profile show|detect|edit`` - what this user actually depends on (phase 3)."""
    action = getattr(args, "action", "show") or "show"
    as_json = bool(getattr(args, "profile_json", False))
    hermes_home = resolve_hermes_home(cfg)
    lang = cfg.language

    if action == "show":
        profile = resolve_profile(cfg, hermes_home=hermes_home)
        if as_json:
            print(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False))
            return EXIT_OK
        source = {
            "config": ("来自你的配置 usage_profile", "from usage_profile in your config"),
            "detected": ("自动检测（可用 `profile edit --write` 固定下来）", "auto-detected (pin it with `profile edit --write`)"),
            "builtin-default": ("内置默认画像", "built-in default profile"),
        }.get(profile.source, (profile.source, profile.source))
        console.heading("使用画像" if lang == "zh" else "USAGE PROFILE")
        console.print(f"  {'来源' if lang == 'zh' else 'source'}: {source[0] if lang == 'zh' else source[1]}")
        console.blank()
        for line in profile.describe_lines(lang=lang):
            console.print(f"  {line}")
        console.blank()
        console.print(
            "  "
            + (
                "critical = 关键（严重回归会阻断）· important = 重要（影响个人影响分）· optional = 可选 · unused = 未使用（完全不计）"
                if lang == "zh"
                else "critical = blocks on a confirmed regression · important = counts toward personal impact · "
                "optional = small weight · unused = never counted"
            )
        )
        console.blank()
        return EXIT_OK

    if action == "detect":
        detection = detect_usage_profile(hermes_home=hermes_home)
        profile = detection.profile
        if as_json:
            print(json.dumps({"profile": profile.to_dict(), "evidence": detection.evidence}, indent=2, ensure_ascii=False))
        else:
            console.heading(
                "检测结果（只区分已配置/未配置，不会替你判断关键程度）"
                if lang == "zh"
                else "DETECTED (configured vs not; it will not guess what is critical)"
            )
            console.blank()
            for line in profile.describe_lines(lang=lang):
                console.print(f"  {line}")
            console.blank()
            if detection.evidence:
                console.print("  " + ("依据：" if lang == "zh" else "evidence:"))
                for key, items in sorted(detection.evidence.items()):
                    console.print(f"    {key:<24} {', '.join(items[:3])}")
                console.blank()
            for note in detection.notes_zh if lang == "zh" else detection.notes_en:
                console.print(f"  · {note}")
            console.blank()
        if getattr(args, "write", False):
            _write_profile(cfg, profile, console, lang=lang)
        return EXIT_OK

    # edit
    profile = resolve_profile(cfg, hermes_home=hermes_home)
    if getattr(args, "write", False):
        _write_profile(cfg, profile, console, lang=lang)
        return EXIT_OK
    path = cfg.source_path or default_config_path()
    console.heading("把下面这段放进配置文件" if lang == "zh" else "PUT THIS BLOCK INTO YOUR CONFIG")
    console.blank()
    console.print(f"  {path}")
    console.blank()
    for line in profile.yaml_block().rstrip().splitlines():
        console.print(f"  {line}")
    console.blank()
    console.print(
        "  "
        + (
            "改好后运行 `hermes-update-check profile show` 确认；"
            "或用 `profile edit --write` 让本工具直接写入（会先备份成 config.yaml.bak，注释会丢失）。"
            if lang == "zh"
            else "verify with `hermes-update-check profile show`, or let the tool write it with "
            "`profile edit --write` (it backs the file up to config.yaml.bak first; comments are lost)."
        )
    )
    console.blank()
    return EXIT_OK


def _write_profile(cfg: Config, profile, console: Console, *, lang: str = "zh") -> None:
    """Write usage_profile into the config file, keeping every other section."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is an install dependency
        console.error(
            "PyYAML is required to write the config" if lang == "en" else "写入配置需要 PyYAML"
        )
        return
    path = cfg.source_path or default_config_path()
    raw = dict(cfg.raw or {})
    raw["usage_profile"] = {"features": dict(sorted(profile.features.items())), "providers": dict(sorted(profile.providers.items()))}
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Optional[Path] = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
    header = (
        "# written by hermes-update-check (profile edit --write)\n"
        "# YAML comments from the previous file were dropped; a backup is at the .bak next to it.\n"
    )
    path.write_text(header + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    console.print(
        (
            f"  已写入 {path}" + (f"（备份：{backup}）" if backup else "")
            if lang == "zh"
            else f"  written to {path}" + (f" (backup: {backup})" if backup else "")
        )
    )
    console.blank()


def cmd_config(args: argparse.Namespace, cfg: Config, console: Console) -> int:
    if args.action == "path":
        path = cfg.source_path or default_config_path()
        print(str(path))
        return EXIT_OK
    if args.action == "init":
        target = write_default_config(Path(args.config).expanduser() if args.config else None, force=bool(args.force))
        console.success(f"config written to {target}")
        return EXIT_OK

    data = cfg.to_dict()
    data["effective_hermes_home"] = str(resolve_hermes_home(cfg))
    data["effective_state_dir"] = str(resolve_state_dir(cfg))
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        console.heading("Configuration" if cfg.language != "zh" else "配置")
        console.print(f"source: {cfg.source_path or '(defaults, no config file)'}")
        console.print(json.dumps(data, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_notify_test(args: argparse.Namespace, cfg: Config, console: Console, logger: Any) -> int:
    notifiers = build_notifiers(cfg)
    if not notifiers:
        console.warn(
            "没有启用任何通知渠道（config.yaml -> notify.telegram.enabled）"
            if cfg.language == "zh"
            else "no notification channel is enabled (see notify.telegram in config.yaml)"
        )
        return EXIT_CONFIG
    message = NotificationMessage(
        title="hermes-update-check test notification",
        body=str(args.message),
        level="INFO",
        fields={"channels": describe_notifiers(notifiers)},
    )
    results = list(notify_all(notifiers, message))
    for result in results:
        console.status_line(f"notify/{result.notifier}", "PASS" if result.ok else "FAIL", result.detail)
    return EXIT_OK if all(r.ok for r in results) else EXIT_ERROR


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _check_exit_code(check: UpdateCheck) -> int:
    """Translate the verdict into a cron-friendly exit code."""
    action = check.action
    if action in {RECOMMEND_UP_TO_DATE, RECOMMEND_AHEAD_OF_STABLE}:
        return EXIT_OK
    if action == RECOMMEND_MANUAL_REVIEW:
        return EXIT_WAIT
    if action == RECOMMEND_UNKNOWN:
        return EXIT_INSUFFICIENT_DATA
    if action in {RECOMMEND_BLOCKED, RECOMMEND_WAIT}:
        return EXIT_WAIT
    # phase-2 names, still accepted (older state files, the legacy assessment path)
    if action == "AVOID":
        return EXIT_WAIT
    if action in {"UPDATE", "UP_TO_DATE"}:
        return EXIT_OK
    # SAFE / ACCEPTABLE mean "updating is a reasonable next step"
    return EXIT_OK


def _one_line_status(check: UpdateCheck, cfg: Config) -> str:
    env = check.env
    prov = check.provenance
    latest = check.latest.tag if check.latest else "?"
    score = check.assessment.score_or_unknown if check.assessment else "UNKNOWN"
    level = check.assessment.level if check.assessment else "UNKNOWN"
    rec = check.action
    age = (
        f"{humanize_hours(check.latest.age_hours, lang=cfg.language)}"
        if check.latest and check.latest.age_hours
        else "?"
    )
    channel = prov.channel if prov else "UNKNOWN"
    gates = ""
    if check.gates is not None and check.gates.blocking:
        gates = " / gates: " + ",".join(g.key for g in check.gates.blocking)
    if cfg.language == "zh":
        return (
            f"[watch] 当前 {env.version_label}（channel {channel}）/ 最新 {latest}（发布 {age} 前）"
            f"/ 风险 {score} {level} / 建议 {rec}{gates} / 无变化，不通知"
        )
    return (
        f"[watch] current {env.version_label} (channel {channel}) / latest {latest} ({age} old) "
        f"/ risk {score} {level} / rec {rec}{gates} / no change, staying quiet"
    )


def _watch_signal(cfg: Config, watch_state, check: UpdateCheck) -> tuple[str, str, bool]:
    """Decide whether this run deserves a notification, and why.

    Only *meaningful* transitions notify: a new release, a risk-band change, a
    hard gate appearing/clearing, a CRITICAL regression appearing/resolving, a
    confidence-bucket change or a channel change. A score moving 92 -> 91 inside
    the same band stays silent.
    """
    from .state import confidence_bucket
    from .util import hours_between, parse_iso8601

    assessment = check.assessment
    current_tag = check.latest.tag if check.latest else None
    current_level = assessment.level if assessment else None
    current_action = check.action
    current_channel = check.channel
    current_status = check.update_status
    gate_blocks = [g.key for g in (check.gates.blocking if check.gates else [])]
    critical_clusters = [c.key for c in check.clusters if c.severity == SEVERITY_CRITICAL]
    current_confidence = (assessment.data_confidence or 0) if assessment else 0

    previous_tag = watch_state.last_latest_tag
    previous_level = watch_state.last_risk_level
    previous_action = watch_state.last_recommendation
    previous_channel = watch_state.last_channel
    previous_status = watch_state.last_update_status
    previous_gates = list(watch_state.last_gate_blocks or [])
    previous_critical = list(watch_state.last_critical_clusters or [])
    previous_bucket = watch_state.last_confidence_bucket

    # throttle
    last_notified = parse_iso8601(watch_state.last_notified_at)
    if (
        last_notified is not None
        and cfg.watch.min_interval_hours
        and hours_between(last_notified) < cfg.watch.min_interval_hours
    ):
        return (
            f"距上次通知不足 {int(cfg.watch.min_interval_hours)} 小时",
            f"less than {int(cfg.watch.min_interval_hours)} h since the last notification",
            False,
        )

    if previous_tag is None:
        if cfg.watch.notify_on_first_run:
            return ("首次运行，已记录状态", "first run: state recorded", True)
        return (
            "首次运行，仅记录状态（notify_on_first_run 为 false）",
            "first run: state recorded (notify_on_first_run is false)",
            False,
        )

    # 1. a new release appeared
    if cfg.watch.notify_on_new_release and current_tag and current_tag != previous_tag:
        return (
            f"发现新 Release：{previous_tag} -> {current_tag}",
            f"new release detected: {previous_tag} -> {current_tag}",
            True,
        )

    # 2. channel changed (e.g. main -> stable)
    if previous_channel and current_channel and current_channel != previous_channel:
        return (
            f"Channel 变化：{previous_channel} -> {current_channel}",
            f"channel changed: {previous_channel} -> {current_channel}",
            True,
        )

    # 3. hard gate state changed
    new_gates = [g for g in gate_blocks if g not in previous_gates]
    cleared_gates = [g for g in previous_gates if g not in gate_blocks]
    if new_gates or cleared_gates:
        if new_gates:
            return (
                f"新的 Hard Gate 触发：{', '.join(new_gates)}",
                f"new hard gate(s) triggered: {', '.join(new_gates)}",
                True,
            )
        return (
            f"Hard Gate 已解除：{', '.join(cleared_gates)}",
            f"hard gate(s) cleared: {', '.join(cleared_gates)}",
            True,
        )

    # 4. CRITICAL regressions appearing or resolving
    new_critical = [c for c in critical_clusters if c not in previous_critical]
    resolved_critical = [c for c in previous_critical if c not in critical_clusters]
    if new_critical:
        return (
            f"新增 CRITICAL 回归：{', '.join(new_critical)}",
            f"new CRITICAL regression(s): {', '.join(new_critical)}",
            True,
        )
    if resolved_critical:
        return (
            f"CRITICAL 回归已消除：{', '.join(resolved_critical)}",
            f"CRITICAL regression(s) resolved: {', '.join(resolved_critical)}",
            True,
        )

    # 6. your critical workflow: newly broken / resolved
    previous_broken = set(watch_state.last_critical_features or [])
    current_broken = {feature.key for feature in (check.readiness.critical_broken if check.readiness else [])}
    if current_broken - previous_broken:
        names = "、".join(sorted(current_broken - previous_broken))
        return (
            f"你的关键工作流出现回归：{names}",
            f"your critical workflow regression detected: {names}",
            True,
        )
    if previous_broken - current_broken:
        names = "、".join(sorted(previous_broken - current_broken))
        return (
            f"关键工作流回归已解除：{names}",
            f"critical workflow regression resolved: {names}",
            True,
        )

    # 5. phase 3: the *personal* verdict changed (doc section 25)
    #
    #   BLOCKED -> WAIT        WAIT -> ACCEPTABLE      ACCEPTABLE -> SAFE
    #   SAFE -> WAIT           ACCEPTABLE -> BLOCKED
    #
    # A global-risk move inside the same verdict (92 -> 91, HIGH -> MEDIUM) is
    # deliberately silent: it changes nothing the user should act on.
    action_order = {
        RECOMMEND_BLOCKED: 0,
        RECOMMEND_WAIT: 1,
        RECOMMEND_UNKNOWN: 1,
        RECOMMEND_MANUAL_REVIEW: 1,
        RECOMMEND_ACCEPTABLE: 2,
        RECOMMEND_SAFE: 3,
    }
    if current_action != previous_action and previous_action and current_action in action_order:
        previous_rank = action_order.get(previous_action)
        current_rank = action_order.get(current_action)
        if previous_rank is not None and current_rank is not None:
            improving = current_rank > previous_rank
            if current_action in {RECOMMEND_SAFE, RECOMMEND_ACCEPTABLE} and cfg.watch.notify_when_safe:
                return (
                    f"{current_tag} 现在可以更新：{previous_action} -> {current_action}",
                    f"{current_tag} is now safe to update: {previous_action} -> {current_action}",
                    True,
                )
            if current_action in {RECOMMEND_BLOCKED, RECOMMEND_WAIT}:
                direction = "改善但仍需等待" if improving else "变得不可更新"
                direction_en = "improved but still waiting" if improving else "became not updatable"
                return (
                    f"建议变化（{direction}）：{previous_action} -> {current_action}",
                    f"recommendation changed ({direction_en}): {previous_action} -> {current_action}",
                    True,
                )

    # 7. confidence bucket change (data quality)
    if previous_bucket and confidence_bucket(current_confidence) != previous_bucket:
        bucket = confidence_bucket(current_confidence)
        return (
            f"数据可信度变化：{previous_bucket} -> {bucket}（{current_confidence}/100）",
            f"data confidence changed: {previous_bucket} -> {bucket} ({current_confidence}/100)",
            True,
        )

    # 8. update status change (e.g. update_available -> ahead_of_stable)
    if previous_status and current_status != previous_status:
        return (
            f"更新状态变化：{previous_status} -> {current_status}",
            f"update status changed: {previous_status} -> {current_status}",
            True,
        )

    return ("无变化", "no change", False)


def _confirm_update(
    args: argparse.Namespace,
    cfg: Config,
    console: Console,
    env: LocalEnv,
    target: str,
    backup: bool,
    risk_label: str,
) -> bool:
    if args.yes:
        return True
    if cfg.auto_update:
        console.warn(
            "auto_update=true 已开启：跳过确认直接更新"
            if cfg.language == "zh"
            else "auto_update=true: proceeding without confirmation"
        )
        return True
    if not sys.stdin.isatty():
        console.warn(
            "非交互式环境且未提供 --yes：拒绝自动更新（这是有意的安全设计）"
            if cfg.language == "zh"
            else "non-interactive stdin and no --yes: refusing to update (this is deliberate)"
        )
        return False
    return _confirm(
        console,
        cfg.language,
        zh=f"风险 {risk_label}；当前 {env.version_label}；目标 {target}；备份 {'已启用' if backup else '已禁用'}。继续？",
        en=f"risk {risk_label}; current {env.version_label}; target {target}; backup {'enabled' if backup else 'disabled'}. Proceed?",
        assume_yes=False,
    )


_LAST_ASSESSMENT: dict[str, Any] = {}


def _confirm(console: Console, lang: str, *, zh: str, en: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    question = zh if lang == "zh" else en
    prompt = f"\n{question} [y/N] "
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("")
        return False
    return answer in {"y", "yes", "是", "确认"}


def _render_preflight(report: PreflightReport, console: Console, lang: str) -> None:
    for check in report.checks:
        detail = check.detail(lang)
        console.status_line(check.name(lang), check.status, f"- {detail}" if detail else "")
        if check.status == STATUS_FAIL:
            remediation = check.remediation(lang)
            if remediation:
                console.print(f"        -> {remediation}")
    console.blank()
    summary = (
        f"通过 {len(report.passed)}，警告 {len(report.warnings)}，失败 {len(report.failures)}"
        if lang == "zh"
        else f"{len(report.passed)} passed, {len(report.warnings)} warning(s), {len(report.failures)} failure(s)"
    )
    console.print(summary)


def _render_health(report: HealthReport, console: Console, lang: str) -> None:
    console.heading("Health check" if lang != "zh" else "健康检查")
    for check in report.checks:
        detail = check.detail(lang)
        console.status_line(check.name(lang), check.status, f"- {detail}" if detail else "")
        if check.status == STATUS_FAIL:
            remediation = check.remediation(lang)
            if remediation:
                console.print(f"        -> {remediation}")
    console.blank()
    console.print(report.summary_line(lang=lang))
    if not report.healthy:
        console.blank()
        console.panel(
            (
                "UPDATE FAILED HEALTH CHECK - 更新后的自检未通过。\n"
                "建议立即回滚： hermes-update-check rollback\n"
                "回滚会恢复旧版本代码并重装依赖（必要时可加 --restore-backup <备份路径>）。"
                if lang == "zh"
                else "UPDATE FAILED HEALTH CHECK - the post-update self test did not pass.\n"
                "Roll back now: hermes-update-check rollback\n"
                "Rollback restores the previous code and reinstalls dependencies (optionally with --restore-backup <path>)."
            ),
            title="HEALTH",
            level=STATUS_FAIL,
        )


def _render_update_outcome(outcome: UpdateOutcome, console: Console, lang: str) -> None:
    console.blank()
    if outcome.env_after is not None:
        console.kv_table(
            [
                ("before", outcome.env_before.version_label),
                ("after", outcome.env_after.version_label),
                ("status", outcome.state.status),
                ("snapshot", outcome.state.snapshot_path or "-"),
                ("update_state", "saved"),
            ]
        )
    if outcome.health is not None:
        _render_health(outcome.health, console, lang)
    if outcome.ok:
        console.success(outcome.message_zh if lang == "zh" else outcome.message_en)
    else:
        console.error(outcome.message_zh if lang == "zh" else outcome.message_en)
        if outcome.result is not None and outcome.result.returncode != 0:
            console.print(f"exit={outcome.result.returncode}")
            console.print(outcome.result.output[-2000:])


def _render_rollback_outcome(outcome: RollbackOutcome, console: Console, lang: str) -> None:
    console.heading("Rollback result" if lang != "zh" else "回滚结果")
    console.bullets(outcome.steps)
    console.blank()
    if outcome.health is not None:
        _render_health(outcome.health, console, lang)
    if outcome.ok:
        console.success(outcome.message_zh if lang == "zh" else outcome.message_en)
    else:
        console.error(outcome.message_zh if lang == "zh" else outcome.message_en)


def _emit(text: str, output: Optional[str], console: Console) -> None:
    if output:
        path = Path(output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        console.success(f"written to {path}")
    else:
        print(text)


def _confirm(console: Console, lang: str, *, zh: str, en: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    question = zh if lang == "zh" else en
    prompt = f"\n{question} [y/N] "
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("")
        return False
    return answer in {"y", "yes", "是", "确认"}


__all__ = ["build_parser", "main"]
