"""Report rendering tests (plain-text console, no network)."""

from __future__ import annotations

from conftest import FakeGitHubClient, make_compare, make_issue, make_release

from hermes_update_check.checker import run_check
from hermes_update_check.console import Console
from hermes_update_check.report import Reporter


def build_check(
    cfg,
    hermes_home,
    state_root,
    *,
    risk_body: str = "fix(state): state.db maintenance",
    issues_ok: bool = True,
    at_tag: bool = True,
):
    """A realistic STABLE install by default (tags_at_head = the release tag)."""
    from test_checker import make_env

    releases = [
        make_release(tag="v2026.9.14", version="0.21.3", age_hours=30, body=risk_body),
        make_release(tag="v2026.9.11", version="0.21.2", age_hours=120),
    ]
    search = {"created:<": (2, []), 'label:"bug"': (1, [])}
    if issues_ok:
        search["in:title"] = (4, [make_issue(1, "[Bug]: state.db corrupt after update")])
    client = FakeGitHubClient(releases=releases, compare=make_compare(commits=60), search=search)
    env = make_env(hermes_home, tags_at_head=["v2026.9.11"] if at_tag else None)
    return run_check(cfg, env=env, client=client, state_root=state_root)


def test_compact_report_mentions_the_essentials(cfg, hermes_home, state_root, capsys) -> None:
    check = build_check(cfg, hermes_home, state_root)
    console = Console(plain=True)
    Reporter(console, lang="zh").render(check, detailed=False)
    out = capsys.readouterr().out
    assert "Hermes Update Advisor" in out
    assert "Release" in out
    assert "最新正式版本" in out
    assert "Risk Level" in out
    assert "建议" in out
    # phase-2 sections
    assert "本地安装状态" in out
    assert "门禁与提示" in out


def test_report_english(cfg, hermes_home, state_root, capsys) -> None:
    cfg.language = "en"
    check = build_check(cfg, hermes_home, state_root)
    Reporter(Console(plain=True), lang="en").render(check, detailed=True)
    out = capsys.readouterr().out
    assert "RECOMMENDATION" in out
    assert "RISK FACTOR BREAKDOWN" in out
    assert "LOCAL ENVIRONMENT" in out
    assert "GATES & CAUTIONS" in out


def test_detailed_report_shows_factors_and_issue_samples(cfg, hermes_home, state_root, capsys) -> None:
    check = build_check(cfg, hermes_home, state_root)
    Reporter(Console(plain=True), lang="zh").render(check, detailed=True)
    out = capsys.readouterr().out
    assert "风险因子明细" in out
    assert "keyword" in out or "变更内容风险" in out
    assert "#1" in out  # issue sample


def test_recommendation_lines_for_wait(cfg, hermes_home, state_root) -> None:
    """The lines follow the advisor verdict (phase 3), not the legacy score."""
    check = build_check(cfg, hermes_home, state_root)
    reporter = Reporter(Console(plain=True), lang="zh")
    lines = reporter.recommendation_lines(check, check.assessment)
    text = "\n".join(lines)
    action = check.recommendation.action if check.recommendation else check.assessment.recommendation
    if action in {"WAIT", "BLOCKED", "INSUFFICIENT_DATA", "MANUAL_REVIEW"}:
        assert "不要更新" in text or "观察" in text
    else:
        assert "可以更新" in text
        assert "备份" in text


def test_recommendation_lines_for_a_blocked_verdict(cfg, hermes_home, state_root) -> None:
    """A BLOCKED verdict must say so and never invite an update."""
    from hermes_update_check.advisor import RECOMMEND_BLOCKED, Recommendation

    check = build_check(cfg, hermes_home, state_root)
    check.recommendation = Recommendation(
        action=RECOMMEND_BLOCKED,
        decided_by="systemic_risk",
        headline_zh="BLOCKED —— 系统级风险",
        headline_en="BLOCKED - systemic critical risk",
        recheck_hours=24.0,
        recheck_at=check.generated_at,
    )
    reporter = Reporter(Console(plain=True), lang="zh")
    text = "\n".join(reporter.recommendation_lines(check, check.assessment))
    assert "不要更新" in text
    assert "观察" in text


def test_markdown_export_contains_tables(cfg, hermes_home, state_root) -> None:
    check = build_check(cfg, hermes_home, state_root)
    md = Reporter(Console(plain=True), lang="zh").to_markdown(check)
    assert md.startswith("# Hermes Update Advisor")
    assert "Update Risk" in md or "Overall Risk" in md
    assert "| factor |" in md or "| 因子 |" in md
    assert check.latest.tag in md


def test_report_survives_missing_issue_data(cfg, hermes_home, state_root, capsys) -> None:
    check = build_check(cfg, hermes_home, state_root, issues_ok=False)
    check.issues = None
    check.assessment.unknown_areas.append("github_issues")
    Reporter(Console(plain=True), lang="zh").render(check, detailed=True)
    out = capsys.readouterr().out
    assert "Hermes Update Advisor" in out


def test_up_to_date_report(cfg, hermes_home, state_root, capsys) -> None:
    from test_checker import make_env

    releases = [make_release(tag="v2026.9.11", version="0.21.2", age_hours=100)]
    client = FakeGitHubClient(releases=releases, compare=make_compare())
    check = run_check(cfg, env=make_env(hermes_home, tags_at_head=["v2026.9.11"]), client=client, state_root=state_root)
    Reporter(Console(plain=True), lang="zh").render(check, detailed=True)
    out = capsys.readouterr().out
    assert "已是最新正式版本" in out


def test_plain_output_has_no_rich_markup(cfg, hermes_home, state_root, capsys) -> None:
    """`--plain` must never leak `[bold]`-style markup (regression: phase 1)."""
    check = build_check(cfg, hermes_home, state_root)
    Reporter(Console(plain=True), lang="zh").render(check, detailed=True)
    out = capsys.readouterr().out
    assert "[bold]" not in out
    assert "[/" not in out
    assert "[yellow]" not in out
    assert "[red]" not in out


def test_json_output_is_markup_free(cfg, hermes_home, state_root) -> None:
    """Machine output must stay parseable: no ANSI, no Rich markup."""
    import json

    check = build_check(cfg, hermes_home, state_root)
    payload = json.dumps(check.to_dict(), ensure_ascii=False)
    assert "\x1b[" not in payload
    assert "[bold]" not in payload and "[/" not in payload
    parsed = json.loads(payload)
    # the phase-2 JSON contract
    for key in ("local", "release", "risk", "hard_gates", "regressions", "recommendation", "recommended_recheck"):
        assert key in parsed
    for key in ("reported_version", "channel", "branch", "commit", "nearest_tag", "ahead_by", "dirty"):
        assert key in parsed["local"]
    for key in ("change_risk", "regression_signal", "data_confidence", "overall"):
        assert key in parsed["risk"]


def test_report_shows_data_freshness_line(cfg, hermes_home, state_root, capsys) -> None:
    """The report must say live vs cached up front (user trust: no silent stale data)."""
    check = build_check(cfg, hermes_home, state_root)
    check.release_data_from_cache = False
    check.release_data_age_seconds = None
    Reporter(Console(plain=True), lang="zh").render(check)
    assert "更新数据：实时" in capsys.readouterr().out

    check.release_data_from_cache = True
    check.release_data_age_seconds = 45 * 60  # 45 minutes
    Reporter(Console(plain=True), lang="en").render(check)
    out = capsys.readouterr().out
    assert "Release data: cached" in out
    assert "45 min" in out
