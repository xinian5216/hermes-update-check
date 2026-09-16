"""Watch-mode delta tests: only meaningful transitions may notify."""

from __future__ import annotations

from pathlib import Path

from conftest import make_release

from hermes_update_check.advisor import Recommendation
from hermes_update_check.checker import UpdateCheck
from hermes_update_check.cli import _watch_signal
from hermes_update_check.clusters import build_clusters
from hermes_update_check.config import Config
from hermes_update_check.gates import GATE_BLOCK, GATE_PASS, GateReport, GateResult
from hermes_update_check.local_env import LocalEnv
from hermes_update_check.provenance import CHANNEL_MAIN, CHANNEL_STABLE, CodeProvenance, UpdateDecision
from hermes_update_check.risk import RiskAssessment
from hermes_update_check.state import WatchState, confidence_bucket


def build_check(
    *,
    tag: str = "v2026.9.14",
    level: str = "HIGH",
    score: int = 70,
    action: str = "WAIT",
    channel: str = CHANNEL_STABLE,
    status: str = "update_available",
    gates: list[tuple[str, str]] | None = None,
    clusters=None,
    confidence: int = 96,
) -> UpdateCheck:
    env = LocalEnv(hermes_home=Path("/tmp/hermes"), install_kind="git", version="0.21.2")
    check = UpdateCheck(cfg=Config(), env=env)
    check.latest = make_release(tag=tag, version="0.21.3", age_hours=30)
    check.provenance = CodeProvenance(channel=channel, reported_version="0.21.2", nearest_tag=tag)
    check.decision = UpdateDecision(status=status, is_update_candidate=status == "update_available")
    check.assessment = RiskAssessment(
        score=score,
        level=level,
        stability=100 - score,
        recommendation=action,
        confidence=confidence / 100.0,
        data_confidence=confidence,
        overall=score,
    )
    report = GateReport(enabled=True)
    for key, gate_status in gates or []:
        report.gates.append(GateResult(key=key, name_zh=key, name_en=key, status=gate_status))
    check.gates = report
    check.clusters = clusters or []
    check.recommendation = Recommendation(action=action, decided_by="hard_gate" if gates else "ok", overall=score)
    return check


def signal(cfg: Config, previous: WatchState, check: UpdateCheck) -> tuple[str, str, bool]:
    return _watch_signal(cfg, previous, check)


def seeded(**kwargs) -> WatchState:
    state = WatchState()
    state.record(
        tag=kwargs.pop("tag", "v2026.9.14"),
        level=kwargs.pop("level", "HIGH"),
        score=kwargs.pop("score", 70),
        recommendation=kwargs.pop("recommendation", "WAIT"),
        channel=kwargs.pop("channel", CHANNEL_STABLE),
        update_status=kwargs.pop("update_status", "update_available"),
        gate_blocks=kwargs.pop("gate_blocks", []),
        critical_clusters=kwargs.pop("critical_clusters", []),
        confidence=kwargs.pop("confidence", 96),
    )
    return state


def test_first_run_is_silent_by_default() -> None:
    cfg = Config()
    reason_zh, _en, notify = signal(cfg, WatchState(), build_check())
    assert notify is False
    assert "首次运行" in reason_zh


def test_first_run_notifies_when_configured() -> None:
    cfg = Config()
    cfg.watch.notify_on_first_run = True
    _zh, _en, notify = signal(cfg, WatchState(), build_check())
    assert notify is True


def test_new_release_notifies() -> None:
    cfg = Config()
    previous = seeded(tag="v2026.9.11")
    _zh, en, notify = signal(cfg, previous, build_check(tag="v2026.9.14"))
    assert notify is True
    assert "new release" in en


def test_same_band_score_wobble_is_silent() -> None:
    """92 -> 91 must not notify: nothing changed that matters."""
    cfg = Config()
    previous = seeded(level="VERY HIGH", score=92, tag="v2026.9.14")
    _zh, en, notify = signal(cfg, previous, build_check(level="VERY HIGH", score=91))
    assert notify is False
    assert en == "no change"


def test_risk_band_change_notifies() -> None:
    cfg = Config()
    previous = seeded(level="VERY HIGH", score=92)
    _zh, en, notify = signal(cfg, previous, build_check(level="HIGH", score=75))
    assert notify is True
    assert "VERY HIGH -> HIGH" in en or "risk level" in en


def test_new_hard_gate_notifies() -> None:
    cfg = Config()
    previous = seeded(gate_blocks=[])
    _zh, en, notify = signal(
        cfg, previous, build_check(gates=[("dirty_worktree", GATE_BLOCK), ("release_age", GATE_BLOCK)])
    )
    assert notify is True
    assert "hard gate" in en.lower()


def test_cleared_hard_gate_notifies() -> None:
    cfg = Config()
    previous = seeded(gate_blocks=["dirty_worktree", "release_age"])
    _zh, en, notify = signal(cfg, previous, build_check(gates=[("dirty_worktree", GATE_PASS)]))
    assert notify is True
    assert "cleared" in en


def test_new_critical_regression_notifies() -> None:
    cfg = Config()
    previous = seeded(critical_clusters=[])
    clusters = build_clusters(
        [
            __import__("test_clusters", fromlist=["issue"]).issue(1, "[Bug] state.db corrupt, data loss", author="a"),
            __import__("test_clusters", fromlist=["issue"]).issue(2, "[Bug] state.db corrupt", author="b"),
        ]
    )
    _zh, en, notify = signal(cfg, previous, build_check(clusters=clusters))
    assert notify is True
    assert "CRITICAL" in en


def test_resolved_critical_regression_notifies() -> None:
    cfg = Config()
    previous = seeded(critical_clusters=["DATABASE"])
    _zh, en, notify = signal(cfg, previous, build_check(clusters=[]))
    assert notify is True
    assert "resolved" in en


def test_channel_change_notifies() -> None:
    cfg = Config()
    previous = seeded(channel=CHANNEL_MAIN)
    _zh, en, notify = signal(cfg, previous, build_check(channel=CHANNEL_STABLE))
    assert notify is True
    assert "channel changed" in en


def test_wait_to_update_notifies() -> None:
    cfg = Config()
    # same risk band, but the action flipped from WAIT to UPDATE
    previous = seeded(recommendation="WAIT", level="MEDIUM", score=38)
    _zh, en, notify = signal(cfg, previous, build_check(action="UPDATE", score=35, level="MEDIUM"))
    assert notify is True
    assert "safe to update" in en


def test_confidence_bucket_change_notifies() -> None:
    cfg = Config()
    previous = seeded(confidence=96)  # HIGH bucket
    _zh, en, notify = signal(cfg, previous, build_check(confidence=40))  # LOW bucket
    assert notify is True
    assert "confidence changed" in en


def test_min_interval_throttles() -> None:
    cfg = Config()
    previous = seeded(tag="v2026.9.11")
    previous.mark_notified("v2026.9.11", "previous")
    _zh, en, notify = signal(cfg, previous, build_check(tag="v2026.9.14"))
    assert notify is False
    assert "last notification" in en


def test_confidence_bucket_boundaries() -> None:
    assert confidence_bucket(100) == "HIGH"
    assert confidence_bucket(85) == "HIGH"
    assert confidence_bucket(84) == "MEDIUM"
    assert confidence_bucket(60) == "MEDIUM"
    assert confidence_bucket(59) == "LOW"
