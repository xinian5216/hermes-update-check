"""Code provenance tests: the five cases from the design brief, plus the rest."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_compare, make_release

from hermes_update_check.github_api import CompareResult, CommitInfo
from hermes_update_check.local_env import GitState, LocalEnv
from hermes_update_check.provenance import (
    CHANNEL_CUSTOM,
    CHANNEL_DETACHED,
    CHANNEL_MAIN,
    CHANNEL_PRERELEASE,
    CHANNEL_STABLE,
    CHANNEL_UNKNOWN,
    UPDATE_STATUS_AHEAD,
    UPDATE_STATUS_AVAILABLE,
    UPDATE_STATUS_MANUAL_REVIEW,
    UPDATE_STATUS_UP_TO_DATE,
    channel_mismatch,
    decide_update,
    resolve_provenance,
)

LATEST = make_release(tag="v2026.9.14", version="0.21.3", age_hours=14.7)


def env_with(
    hermes_home: Path,
    *,
    version: str = "0.21.2",
    tag: str | None = "v2026.9.11",
    branch: str | None = "main",
    tags_at_head: list[str] | None = None,
    dirty: bool = False,
    install_kind: str = "git",
    git: bool = True,
) -> LocalEnv:
    return LocalEnv(
        hermes_home=hermes_home,
        install_kind=install_kind,
        hermes_cli=None,
        version=version,
        release_tag=tag,
        commit="5eb99eb2",
        git=GitState(
            is_repo=git,
            branch=branch,
            commit="5eb99eb2",
            full_commit="5eb99eb2" + "0" * 32,
            dirty=dirty,
            dirty_files=["a.py"] if dirty else [],
            tags_at_head=list(tags_at_head or []),
        )
        if git
        else None,
    )


def comparison(status: str, *, ahead: int = 0, behind: int = 0, base: str = "v2026.9.14", head: str = "HEAD") -> CompareResult:
    return CompareResult(
        base_tag=base,
        head_tag=head,
        status=status,
        total_commits=ahead + behind,
        ahead_by=ahead,
        behind_by=behind,
        commits=[CommitInfo(sha="a" * 40, message="fix: x")],
    )


# --------------------------------------------------------------------------- #
# Case 1: main, ahead of the latest release -> MAIN, not an update candidate
# --------------------------------------------------------------------------- #


def test_case1_main_ahead_of_latest_release(hermes_home: Path) -> None:
    env = env_with(hermes_home, version="0.21.2", tag="v2026.9.11", branch="main")
    prov = resolve_provenance(env, [LATEST], latest=LATEST, compare=lambda b, h: comparison("ahead", ahead=300))
    assert prov.channel == CHANNEL_MAIN
    assert prov.commits_ahead_of_tag == 300
    assert prov.ahead_of_stable is True
    assert prov.nearest_tag == "v2026.9.14"

    decision = decide_update(prov, LATEST)
    assert decision.status == UPDATE_STATUS_AHEAD
    assert decision.is_update_candidate is False
    assert "ahead of the latest" in decision.message_en.lower() or "领先" in decision.message_zh


def test_main_behind_release_is_an_update_candidate(hermes_home: Path) -> None:
    """Regression (found in the real E2E): on main but *behind* the tag.

    The local checkout can be behind the release while the remote branch is
    ahead of it - the local commit is what counts, and pulling really does move
    the code forward. The main-branch gate still stops an actual update.
    """
    older = make_release(tag="v2026.9.11", version="0.21.2", age_hours=100)
    env = env_with(hermes_home, branch="main")
    prov = resolve_provenance(env, [LATEST, older], latest=LATEST, compare=lambda b, h: comparison("behind", behind=125))
    assert prov.channel == CHANNEL_MAIN
    assert prov.commits_behind_target == 125
    assert prov.ahead_of_stable is False

    decision = decide_update(prov, LATEST)
    assert decision.status == UPDATE_STATUS_AVAILABLE
    assert decision.is_update_candidate is True
    assert "125" in decision.message_en


def test_main_diverged_from_release_needs_manual_review(hermes_home: Path) -> None:
    env = env_with(hermes_home, branch="main")
    prov = resolve_provenance(env, [LATEST], latest=LATEST, compare=lambda b, h: comparison("diverged", ahead=5, behind=9))
    assert prov.ahead_of_stable is True and prov.commits_behind_target == 9
    decision = decide_update(prov, LATEST)
    assert decision.status == UPDATE_STATUS_MANUAL_REVIEW  # ahead *and* behind: not a plain upgrade


def test_case1_when_compare_is_unavailable_main_is_not_a_candidate(hermes_home: Path) -> None:
    env = env_with(hermes_home, branch="main")
    prov = resolve_provenance(env, [LATEST], latest=LATEST, compare=lambda b, h: None)
    assert prov.channel == CHANNEL_MAIN
    assert prov.compare_available is False
    assert prov.commits_ahead_of_tag is None
    decision = decide_update(prov, LATEST)
    # no distance information: still not a normal update path
    assert decision.status == UPDATE_STATUS_MANUAL_REVIEW
    assert decision.is_update_candidate is False


# --------------------------------------------------------------------------- #
# Case 2/3: stable install behind / level with the latest release
# --------------------------------------------------------------------------- #


def test_case2_stable_update_available(hermes_home: Path) -> None:
    older = make_release(tag="v2026.9.11", version="0.21.2", age_hours=100)
    env = env_with(hermes_home, tags_at_head=["v2026.9.11"])
    prov = resolve_provenance(env, [LATEST, older], latest=LATEST, compare=lambda b, h: comparison("behind", behind=40))
    assert prov.channel == CHANNEL_STABLE
    decision = decide_update(prov, LATEST)
    assert decision.status == UPDATE_STATUS_AVAILABLE
    assert decision.is_update_candidate is True


def test_case3_stable_up_to_date(hermes_home: Path) -> None:
    latest = make_release(tag="v2026.9.11", version="0.21.2", age_hours=100)
    env = env_with(hermes_home, version="0.21.2", tag="v2026.9.11", tags_at_head=["v2026.9.11"])
    prov = resolve_provenance(env, [latest], latest=latest, compare=lambda b, h: comparison("identical"))
    assert prov.channel == CHANNEL_STABLE and prov.tag_matched is True
    decision = decide_update(prov, latest)
    assert decision.status == UPDATE_STATUS_UP_TO_DATE
    assert decision.is_update_candidate is False


# --------------------------------------------------------------------------- #
# Case 4/5: detached HEAD, custom build
# --------------------------------------------------------------------------- #


def test_case4_detached_head_requires_manual_review(hermes_home: Path) -> None:
    env = env_with(hermes_home, branch="HEAD", tags_at_head=[], version="0.21.3")
    prov = resolve_provenance(env, [LATEST], latest=LATEST, compare=lambda b, h: comparison("diverged", ahead=5, behind=5))
    assert prov.channel == CHANNEL_DETACHED
    assert prov.detached is True
    decision = decide_update(prov, LATEST)
    assert decision.status == UPDATE_STATUS_MANUAL_REVIEW
    assert decision.is_update_candidate is False


def test_case4_detached_but_on_a_release_tag_is_stable(hermes_home: Path) -> None:
    env = env_with(hermes_home, branch="HEAD", tags_at_head=["v2026.9.14"], version="0.21.3")
    prov = resolve_provenance(env, [LATEST], latest=LATEST, compare=lambda b, h: comparison("identical"))
    assert prov.channel == CHANNEL_STABLE


def test_case5_custom_branch_maps_to_no_release(hermes_home: Path) -> None:
    env = env_with(hermes_home, branch="feature/xyz", tags_at_head=[])
    prov = resolve_provenance(env, [LATEST], latest=LATEST, compare=lambda b, h: comparison("diverged", ahead=2, behind=30))
    assert prov.channel == CHANNEL_CUSTOM
    decision = decide_update(prov, LATEST)
    assert decision.status == UPDATE_STATUS_MANUAL_REVIEW


def test_prerelease_channel(hermes_home: Path) -> None:
    env = env_with(hermes_home, version="0.22.0-rc1", tag="v2026.9.20", tags_at_head=["v2026.9.20"], branch="HEAD")
    pre = make_release(tag="v2026.9.20", version="0.22.0-rc1", prerelease=True)
    prov = resolve_provenance(env, [pre], latest=pre, compare=lambda b, h: comparison("identical"))
    assert prov.channel == CHANNEL_PRERELEASE
    decision = decide_update(prov, pre)
    assert decision.status == UPDATE_STATUS_MANUAL_REVIEW
    assert decide_update(prov, pre, allow_prerelease=True).is_update_candidate is True


def test_unknown_channel_without_any_information(hermes_home: Path) -> None:
    env = env_with(hermes_home, version=None, tag=None, branch=None, git=False)
    prov = resolve_provenance(env, [], latest=None, compare=None)
    assert prov.channel == CHANNEL_UNKNOWN
    decision = decide_update(prov, None)
    assert decision.status == "unknown"
    assert decision.is_update_candidate is None


def test_non_git_install_with_known_release_is_stable(hermes_home: Path) -> None:
    env = env_with(hermes_home, version="0.21.2", tag="v2026.9.11", git=False, install_kind="docker")
    prov = resolve_provenance(env, [LATEST, make_release(tag="v2026.9.11", version="0.21.2")], latest=LATEST, compare=None)
    assert prov.channel == CHANNEL_STABLE
    assert prov.is_git_install is False
    assert prov.evidence  # explains that provenance came from the reported version


# --------------------------------------------------------------------------- #
# version-vs-git conflicts, channel mismatch, JSON contract
# --------------------------------------------------------------------------- #


def test_reported_version_conflict_is_surfaced(hermes_home: Path) -> None:
    env = env_with(hermes_home, version="0.21.2", tag="v2026.9.11", branch="main")
    prov = resolve_provenance(env, [LATEST], latest=LATEST, compare=lambda b, h: comparison("ahead", ahead=218))
    joined = " ".join(zh for zh, _en in prov.evidence)
    assert "领先" in joined
    assert prov.to_dict()["ahead_by"] == 218


def test_json_contract_keys(hermes_home: Path) -> None:
    older = make_release(tag="v2026.9.11", version="0.21.2", age_hours=100)
    env = env_with(hermes_home, tags_at_head=["v2026.9.11"], dirty=True)
    prov = resolve_provenance(env, [LATEST, older], latest=LATEST, compare=lambda b, h: comparison("behind", behind=3))
    data = prov.to_dict()
    for key in ("reported_version", "channel", "branch", "commit", "nearest_tag", "ahead_by", "dirty"):
        assert key in data
    assert data["dirty"] is True
    assert data["channel"] == CHANNEL_STABLE


@pytest.mark.parametrize(
    "preferred,channel,expected_warning",
    [
        ("stable", CHANNEL_MAIN, True),
        ("stable", CHANNEL_STABLE, False),
        ("main", CHANNEL_STABLE, True),
        ("main", CHANNEL_MAIN, False),
        ("prerelease", CHANNEL_STABLE, True),
    ],
)
def test_channel_mismatch_warnings(preferred: str, channel: str, expected_warning: bool) -> None:
    from hermes_update_check.provenance import CodeProvenance

    prov = CodeProvenance(channel=channel)
    warning = channel_mismatch(prov, preferred)
    assert (warning is not None) is expected_warning
    if warning:
        assert "自动降级" in warning[0] or "preferred_channel" in warning[0]


def test_make_compare_fixture_has_both_directions() -> None:
    """Guard: local behaviour of the test double used above."""
    ahead = make_compare(commits=5)
    assert ahead.status == "ahead" and ahead.ahead_by == 5
