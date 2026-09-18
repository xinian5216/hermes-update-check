"""Phase-4 integration: gates, update transaction, rollback, watch, provenance.

These are the phase-4 scenarios from the specification that live *outside* the
overrides module itself: what the rest of the tool does once a registry exists.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import make_release

from hermes_update_check import overrides as ov
from hermes_update_check.config import Config
from hermes_update_check.gates import GATE_BLOCK, GATE_PASS, GATE_WARN, evaluate_gates
from hermes_update_check.local_env import GitState, LocalEnv
from hermes_update_check.provenance import (
    CHANNEL_MAIN,
    CHANNEL_STABLE,
    COMPARE_SOURCE_LOCAL_GIT,
    INSTALL_FORK,
    INSTALL_MAIN_CLEAN,
    INSTALL_MAIN_WITH_MANAGED_OVERRIDES,
    INSTALL_STANDARD_RELEASE,
    UPDATE_STATUS_AVAILABLE,
    CodeProvenance,
    UpdateDecision,
    _install_state,
    _looks_like_fork,
    local_git_relation,
    resolve_provenance,
)
from hermes_update_check.state import (
    STAGE_CLEANED,
    STAGE_COMMITTED,
    STAGE_PREPARED,
    STAGE_UPDATED,
    STATUS_SUCCEEDED,
    StateStore,
    UpdateState,
)

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git executable not available")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    )
    return result.stdout


def _commit(repo: Path, message: str = "commit") -> str:
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    if GIT is None:  # pragma: no cover
        pytest.skip("git executable not available")
    root = tmp_path / "install"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "hermes").mkdir()
    (root / "hermes" / "a.py").write_text("print('a')\nline2\nline3\n", encoding="utf-8")
    (root / "hermes" / "b.py").write_text("print('b')\n", encoding="utf-8")
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(root, "add", "-A")
    _commit(root, "initial")
    return root


@pytest.fixture()
def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir()
    return root


def _env(repo: Path, hermes_home: Path) -> LocalEnv:
    env = LocalEnv(hermes_home=hermes_home, install_kind="git", install_dir=repo)
    env.git = GitState(
        is_repo=True,
        branch="main",
        commit="0bca6a32",
        full_commit="0bca6a3200000000000000000000000000000000",
        dirty=True,
        dirty_files=["hermes/a.py"],
        remote_url="https://github.com/NousResearch/hermes-agent.git",
    )
    return env


def _decision() -> UpdateDecision:
    return UpdateDecision(status=UPDATE_STATUS_AVAILABLE, is_update_candidate=True, target_tag="v2026.9.14")


def _provenance(**kwargs) -> CodeProvenance:
    defaults = {"channel": CHANNEL_MAIN, "is_git_install": True, "dirty_worktree": True, "dirty_files": 1}
    defaults.update(kwargs)
    return CodeProvenance(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# gates: known dirty vs unknown dirty (doc section 14)
# --------------------------------------------------------------------------- #


def test_gate_passes_with_only_managed_overrides(cfg: Config) -> None:
    report = ov.OverrideReport(
        managed=[ov.LocalChange(path="hermes/a.py", status=ov.STATUS_MODIFIED)],
        managed_exact=["hermes/a.py"],
        registry=ov.OverrideRegistry(files=[ov.OverrideEntry(path="hermes/a.py", match="exact")]),
        safety=ov.SAFETY_PASS,
    )
    gates = evaluate_gates(
        cfg, provenance=_provenance(), decision=_decision(), release=None, assessment=None, overrides=report
    )
    gate = next(g for g in gates.gates if g.key == "dirty_worktree")
    assert gate.status == GATE_PASS
    assert "1 个本地定制" in gate.reason_zh


def test_gate_blocks_unknown_changes(cfg: Config) -> None:
    report = ov.OverrideReport(
        managed=[ov.LocalChange(path="hermes/a.py", status=ov.STATUS_MODIFIED)],
        managed_exact=["hermes/a.py"],
        unknown=[ov.LocalChange(path="hermes/b.py", status=ov.STATUS_MODIFIED)],
        registry=ov.OverrideRegistry(files=[ov.OverrideEntry(path="hermes/a.py")]),
        safety=ov.SAFETY_FAIL,
    )
    gates = evaluate_gates(
        cfg, provenance=_provenance(), decision=_decision(), release=None, assessment=None, overrides=report
    )
    gate = next(g for g in gates.gates if g.key == "dirty_worktree")
    assert gate.status == GATE_BLOCK
    assert "未登记" in gate.reason_zh


def test_gate_blocks_drift_but_can_be_relaxed(cfg: Config) -> None:
    report = ov.OverrideReport(
        managed=[ov.LocalChange(path="hermes/a.py", status=ov.STATUS_MODIFIED)],
        drifted=["hermes/a.py"],
        registry=ov.OverrideRegistry(files=[ov.OverrideEntry(path="hermes/a.py")]),
        safety=ov.SAFETY_WARN,
    )
    gates = evaluate_gates(
        cfg, provenance=_provenance(), decision=_decision(), release=None, assessment=None, overrides=report
    )
    assert next(g for g in gates.gates if g.key == "dirty_worktree").status == GATE_BLOCK

    cfg.hard_gates.block_drifted_overrides = False
    gates = evaluate_gates(
        cfg, provenance=_provenance(), decision=_decision(), release=None, assessment=None, overrides=report
    )
    gate = next(g for g in gates.gates if g.key == "dirty_worktree")
    assert gate.status == GATE_WARN


def test_gate_without_registry_keeps_the_phase3_behaviour(cfg: Config) -> None:
    report = ov.OverrideReport(
        unknown=[ov.LocalChange(path="hermes/a.py", status=ov.STATUS_MODIFIED)], safety=ov.SAFETY_FAIL
    )
    gates = evaluate_gates(
        cfg, provenance=_provenance(), decision=_decision(), release=None, assessment=None, overrides=report
    )
    gate = next(g for g in gates.gates if g.key == "dirty_worktree")
    assert gate.status == GATE_BLOCK
    assert "overrides register" in gate.remediation_zh


def test_gate_clean_tree_is_unchanged(cfg: Config) -> None:
    gates = evaluate_gates(
        cfg,
        provenance=_provenance(dirty_worktree=False, dirty_files=0),
        decision=_decision(),
        release=None,
        assessment=None,
        overrides=ov.OverrideReport(),
    )
    gate = next(g for g in gates.gates if g.key == "dirty_worktree")
    assert gate.status == GATE_PASS
    assert gate.reason_zh == "工作区干净"


# --------------------------------------------------------------------------- #
# transaction stages (doc section 20)
# --------------------------------------------------------------------------- #


def test_update_state_transaction_stages_and_interruption() -> None:
    state = UpdateState(previous_commit="abc", update_time="2026-09-18T00:00:00Z")
    assert state.interrupted is False
    state.advance(STAGE_PREPARED)
    state.advance(STAGE_CLEANED)
    assert state.interrupted is True
    assert "CLEANED" in (state.interrupted_summary(lang="zh") or "")
    state.advance(STAGE_UPDATED)
    state.advance(STAGE_COMMITTED)
    state.status = STATUS_SUCCEEDED
    assert state.interrupted is False
    assert state.interrupted_summary() is None
    assert state.stage_history == [STAGE_PREPARED, STAGE_CLEANED, STAGE_UPDATED, STAGE_COMMITTED]
    payload = state.to_dict()
    assert payload["stage"] == STAGE_COMMITTED


def test_update_state_round_trips_the_new_fields(tmp_path: Path) -> None:
    state = UpdateState(
        overrides_before=["hermes/a.py"],
        overrides_patch_file="/tmp/x.patch",
        overrides_patch_sha256="deadbeef",
        overrides_reapplied=["hermes/a.py"],
        overrides_conflicts=[],
    )
    state.advance(STAGE_UPDATED)
    store = StateStore(tmp_path)
    store.save_update_state(state)
    loaded = store.load_update_state()
    assert loaded is not None
    assert loaded.overrides_before == ["hermes/a.py"]
    assert loaded.stage == STAGE_UPDATED
    assert loaded.interrupted is True


# --------------------------------------------------------------------------- #
# provenance: fork / upstream / install state (doc sections 29-34)
# --------------------------------------------------------------------------- #


def test_looks_like_fork_and_official_remote() -> None:
    assert _looks_like_fork("https://github.com/NousResearch/hermes-agent.git", "NousResearch/hermes-agent") is False
    assert _looks_like_fork("git@github.com:someone/hermes-agent.git", "NousResearch/hermes-agent") is True
    assert _looks_like_fork("https://github.com/someone/hermes-agent", "NousResearch/hermes-agent") is True
    assert _looks_like_fork(None, "NousResearch/hermes-agent") is False


def test_install_state_classification() -> None:
    git = GitState(is_repo=True, branch="main", remote_url="https://github.com/NousResearch/hermes-agent.git")
    main = _provenance(channel=CHANNEL_MAIN)
    assert _install_state(main, git, managed=3, unknown=0) == INSTALL_MAIN_WITH_MANAGED_OVERRIDES
    assert _install_state(main, git, managed=0, unknown=0) == INSTALL_MAIN_CLEAN

    stable = _provenance(channel=CHANNEL_STABLE, dirty_worktree=False)
    assert _install_state(stable, git, managed=0, unknown=0) == INSTALL_STANDARD_RELEASE

    fork_prov = _provenance(channel=CHANNEL_MAIN, fork_detected=True)
    assert _install_state(fork_prov, git, managed=0, unknown=0) == INSTALL_FORK

    non_git = _provenance(is_git_install=False, channel=CHANNEL_STABLE)
    assert _install_state(non_git, git, 0, 0) == INSTALL_STANDARD_RELEASE


def test_local_only_commit_on_main_is_not_manual_review(hermes_home: Path) -> None:
    """The real case: main + an unpushed commit + a 404 compare (doc section 29)."""
    env = LocalEnv(hermes_home=hermes_home, install_kind="git", version="0.21.2", release_tag="v2026.9.11")
    env.git = GitState(
        is_repo=True,
        branch="main",
        commit="0bca6a32",
        full_commit="0bca6a3200000000000000000000000000000000",
        remote_url="https://github.com/NousResearch/hermes-agent.git",
    )
    prov = resolve_provenance(
        env,
        [make_release(tag="v2026.9.14", version="0.21.3")],
        latest=make_release(tag="v2026.9.14", version="0.21.3"),
        compare=lambda base, head: None,
        managed_overrides=4,
        unknown_changes=0,
    )
    assert prov.install_state == INSTALL_MAIN_WITH_MANAGED_OVERRIDES
    assert prov.compare_available is False
    assert prov.fork_detected is False


@needs_git
def test_fork_with_upstream_remote_is_not_anomalous(tmp_path: Path, hermes_home: Path) -> None:
    repo = tmp_path / "fork"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _commit(repo, "one")
    _git(repo, "remote", "add", "origin", "git@github.com:someone/hermes-agent.git")
    _git(repo, "remote", "add", "upstream", "https://github.com/NousResearch/hermes-agent.git")

    env = LocalEnv(hermes_home=hermes_home, install_kind="git", install_dir=repo, release_tag="v2026.9.14")
    env.git = GitState(
        is_repo=True,
        branch="main",
        commit="abc1234",
        full_commit="abc1234000000000000000000000000000000000",
        remote_url="git@github.com:someone/hermes-agent.git",
    )
    prov = resolve_provenance(env, [], latest=None, compare=lambda base, head: None)
    assert prov.fork_detected is True
    assert prov.install_state == INSTALL_FORK
    assert any("fork" in en for _zh, en in prov.evidence)


@needs_git
def test_local_git_relation_measures_ahead_and_behind(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _commit(repo, "one")
    first = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "a.txt").write_text("2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _commit(repo, "two")

    assert local_git_relation(repo, first) == (0, 1)  # behind 0, ahead 1
    _git(repo, "checkout", "-q", first)
    assert local_git_relation(repo, first) == (0, 0)
    assert local_git_relation(repo, "no-such-ref") is None


@needs_git
def test_behind_release_is_an_update_candidate_via_local_git(tmp_path: Path, hermes_home: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _commit(repo, "one")
    release_commit = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "reset", "-q", "--hard", "HEAD~0")
    # move the local branch back one commit so it is *behind* the release commit
    (repo / "b.txt").write_text("2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _commit(repo, "newer release commit")

    env = LocalEnv(hermes_home=hermes_home, install_kind="git", install_dir=repo, release_tag="v2026.9.11")
    env.git = GitState(is_repo=True, branch="main", commit=release_commit[:8], full_commit=release_commit)
    _git(repo, "checkout", "-q", "-B", "main", release_commit)
    prov = resolve_provenance(
        env,
        [make_release(tag="v2026.9.14", version="0.21.3")],
        latest=make_release(tag="v2026.9.14", version="0.21.3"),
        compare=lambda base, head: None,
        target_commit=_git(repo, "rev-parse", "main").strip(),
    )
    assert prov.compare_source == COMPARE_SOURCE_LOCAL_GIT


# --------------------------------------------------------------------------- #
# update + rollback keep the local customization (doc sections 15-19, 43)
# --------------------------------------------------------------------------- #


@needs_git
def test_update_aborts_on_unknown_changes(repo: Path, state_root: Path, hermes_home: Path) -> None:
    from hermes_update_check.updater import run_update

    (repo / "hermes" / "a.py").write_text("custom\n", encoding="utf-8")
    ov.register_overrides(repo, state_root, paths=["hermes/a.py"])
    (repo / "hermes" / "b.py").write_text("surprise\n", encoding="utf-8")

    cfg = Config()
    env = _env(repo, hermes_home)
    outcome = run_update(cfg, env, state_root=state_root, yes=True, skip_health_check=True)
    assert outcome.ok is False
    assert outcome.overrides_blocked is True
    assert outcome.overrides is not None and outcome.overrides.unknown_count == 1
    assert "未登记" in outcome.message_zh
    # nothing was executed: the user's edits are all still there
    assert "custom" in (repo / "hermes" / "a.py").read_text(encoding="utf-8")
    assert "surprise" in (repo / "hermes" / "b.py").read_text(encoding="utf-8")


@needs_git
def test_update_aborts_on_drift(repo: Path, state_root: Path, hermes_home: Path) -> None:
    from hermes_update_check.updater import run_update

    (repo / "hermes" / "a.py").write_text("v1\n", encoding="utf-8")
    ov.register_overrides(repo, state_root, paths=["hermes/a.py"])
    (repo / "hermes" / "a.py").write_text("v2\n", encoding="utf-8")

    outcome = run_update(Config(), _env(repo, hermes_home), state_root=state_root, yes=True, skip_health_check=True)
    assert outcome.overrides_blocked is True
    assert "drift" in (outcome.message_zh + outcome.message_en)


@needs_git
def test_rollback_refuses_with_unknown_changes(repo: Path, state_root: Path, hermes_home: Path) -> None:
    from hermes_update_check.errors import CommandError
    from hermes_update_check.updater import run_rollback

    (repo / "hermes" / "a.py").write_text("custom\n", encoding="utf-8")
    ov.register_overrides(repo, state_root, paths=["hermes/a.py"])
    (repo / "hermes" / "b.py").write_text("unknown\n", encoding="utf-8")

    store = StateStore(state_root)
    state = UpdateState(
        previous_commit=_git(repo, "rev-parse", "HEAD").strip(), install_dir=str(repo), hermes_home=str(hermes_home)
    )
    store.save_update_state(state)
    with pytest.raises(CommandError):
        run_rollback(Config(), _env(repo, hermes_home), store=store, state=state, yes=True, dry_run=False)


@needs_git
def test_rollback_reapplies_overrides_after_checkout(repo: Path, state_root: Path, hermes_home: Path) -> None:
    """Doc section 43: a rollback must not silently drop the user's customization."""
    from hermes_update_check.updater import run_rollback

    (repo / "hermes" / "a.py").write_text("print('a')\nline2 LOCAL\nline3\n", encoding="utf-8")
    ov.register_overrides(repo, state_root, paths=["hermes/a.py"])
    old_commit = _git(repo, "rev-parse", "HEAD").strip()
    # simulate an update: commit the customization upstream, then "lose" it
    _git(repo, "checkout", "-q", "--", "hermes/a.py")
    (repo / "hermes" / "a.py").write_text("print('a')\nline2 NEW_VERSION\nline3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _commit(repo, "newer version")

    store = StateStore(state_root)
    state = UpdateState(previous_commit=old_commit, install_dir=str(repo), hermes_home=str(hermes_home))
    store.save_update_state(state)
    cfg = Config()
    cfg.update.health_check_after_update = False  # the fake repo has no Hermes to health-check
    outcome = run_rollback(cfg, _env(repo, hermes_home), store=store, state=state, yes=True, reinstall_deps=False)
    assert outcome.ok is True
    steps = " ".join(outcome.steps)
    assert "local overrides reapplied" in steps
    content = (repo / "hermes" / "a.py").read_text(encoding="utf-8")
    assert "line2 LOCAL" in content


# --------------------------------------------------------------------------- #
# watch: stable overrides stay silent (doc section 46)
# --------------------------------------------------------------------------- #


def _check_with_overrides(**kwargs):
    """A minimal stand-in for UpdateCheck with just what the watch signal reads."""
    from hermes_update_check.checker import UpdateCheck

    check = UpdateCheck(cfg=Config(), env=LocalEnv(hermes_home=Path(".")))
    check.overrides = kwargs.get("overrides")
    check.latest = kwargs.get("latest")
    check.decision = kwargs.get("decision") or UpdateDecision(
        status=UPDATE_STATUS_AVAILABLE, is_update_candidate=True, target_tag="v2026.9.14"
    )
    return check


def test_watch_stays_silent_for_stable_managed_overrides(cfg: Config) -> None:
    from hermes_update_check.cli import _watch_signal
    from hermes_update_check.state import WatchState

    watch = WatchState(
        last_latest_tag="v2026.9.14",
        last_recommendation="ACCEPTABLE",
        last_update_status="update_available",
        last_override_unknown=0,
        last_override_drifted=0,
        last_reapply_confidence="HIGH",
    )
    report = ov.OverrideReport(
        managed=[ov.LocalChange(path="a.py", status=ov.STATUS_MODIFIED)],
        managed_exact=["a.py"],
        registry=ov.OverrideRegistry(files=[ov.OverrideEntry(path="a.py")]),
        safety=ov.SAFETY_PASS,
        prediction=ov.ReapplyPrediction(confidence="HIGH", target="v2026.9.14"),
    )
    check = _check_with_overrides(overrides=report)
    check.latest = make_release(tag="v2026.9.14", version="0.21.3")
    check.provenance = CodeProvenance(channel=CHANNEL_MAIN)
    reason_zh, _reason_en, notify = _watch_signal(cfg, watch, check)
    assert notify is False, reason_zh


def test_watch_alerts_on_drift(cfg: Config) -> None:
    from hermes_update_check.cli import _watch_signal
    from hermes_update_check.state import WatchState

    watch = WatchState(
        last_latest_tag="v2026.9.14",
        last_recommendation="ACCEPTABLE",
        last_update_status="update_available",
        last_override_unknown=0,
        last_override_drifted=0,
    )
    report = ov.OverrideReport(
        managed=[ov.LocalChange(path="a.py", status=ov.STATUS_MODIFIED)],
        drifted=["a.py"],
        registry=ov.OverrideRegistry(files=[ov.OverrideEntry(path="a.py")]),
        safety=ov.SAFETY_WARN,
    )
    check = _check_with_overrides(overrides=report)
    check.latest = make_release(tag="v2026.9.14", version="0.21.3")
    check.provenance = CodeProvenance(channel=CHANNEL_MAIN)
    reason_zh, _en, notify = _watch_signal(cfg, watch, check)
    assert notify is True
    assert "漂移" in reason_zh


def test_watch_alerts_on_new_unknown_changes(cfg: Config) -> None:
    from hermes_update_check.cli import _watch_signal
    from hermes_update_check.state import WatchState

    watch = WatchState(
        last_latest_tag="v2026.9.14",
        last_recommendation="ACCEPTABLE",
        last_update_status="update_available",
        last_override_unknown=0,
    )
    report = ov.OverrideReport(
        unknown=[ov.LocalChange(path="x.py", status=ov.STATUS_MODIFIED)],
        safety=ov.SAFETY_FAIL,
    )
    check = _check_with_overrides(overrides=report)
    check.latest = make_release(tag="v2026.9.14", version="0.21.3")
    check.provenance = CodeProvenance(channel=CHANNEL_MAIN)
    _zh, _en, notify = _watch_signal(cfg, watch, check)
    assert notify is True


def test_watch_alerts_when_reapply_confidence_drops(cfg: Config) -> None:
    from hermes_update_check.cli import _watch_signal
    from hermes_update_check.state import WatchState

    watch = WatchState(
        last_latest_tag="v2026.9.14",
        last_recommendation="ACCEPTABLE",
        last_update_status="update_available",
        last_override_unknown=0,
        last_override_drifted=0,
        last_reapply_confidence="HIGH",
    )
    report = ov.OverrideReport(
        managed=[ov.LocalChange(path="a.py", status=ov.STATUS_MODIFIED)],
        managed_exact=["a.py"],
        registry=ov.OverrideRegistry(files=[ov.OverrideEntry(path="a.py")]),
        safety=ov.SAFETY_PASS,
        prediction=ov.ReapplyPrediction(confidence="LOW", target="v2026.9.14"),
    )
    check = _check_with_overrides(overrides=report)
    check.latest = make_release(tag="v2026.9.14", version="0.21.3")
    check.provenance = CodeProvenance(channel=CHANNEL_MAIN)
    reason_zh, _en, notify = _watch_signal(cfg, watch, check)
    assert notify is True
    assert "HIGH -> LOW" in reason_zh


# --------------------------------------------------------------------------- #
# output: JSON stays machine readable and never leaks a patch
# --------------------------------------------------------------------------- #


def test_override_report_to_dict_is_json_serialisable() -> None:
    report = ov.OverrideReport(
        managed=[ov.LocalChange(path="a.py", status=ov.STATUS_MODIFIED)],
        unknown=[ov.LocalChange(path="b.py", status=ov.STATUS_UNTRACKED, untracked=True)],
        drifted=["c.py"],
        missing=["d.py"],
        managed_exact=["a.py"],
        registry=ov.OverrideRegistry(files=[ov.OverrideEntry(path="a.py")]),
        safety=ov.SAFETY_FAIL,
        prediction=ov.ReapplyPrediction(confidence="MEDIUM", target="v2026.9.14", conflicts=["c.py"]),
    )
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["managed_count"] == 1
    assert payload["unknown_count"] == 1
    assert payload["drifted"] == ["c.py"]
    assert payload["safety"] == "FAIL"
    assert payload["prediction"]["manual_merge_likely"] is False


def test_patch_sensitivity_guard(tmp_path: Path) -> None:
    """Doc section 41: warn about secrets in a patch but never delete it."""
    from hermes_update_check.cli import _patch_looks_sensitive

    clean = "diff --git a/x.py b/x.py\n+print('hello')\n"
    assert _patch_looks_sensitive(clean) is False
    leaky = 'diff --git a/x.py b/x.py\n+TOKEN = "ghp_' + "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6" + '"\n'
    assert _patch_looks_sensitive(leaky) is True
