"""Code provenance: *what code is actually running*, not just what it calls itself.

Why this exists
---------------

``hermes --version`` reports the version the *source tree* declares, which says
nothing about where that tree sits relative to releases:

* branch ``main`` with 218 commits on top of the latest release tag still
  declares version ``0.21.2`` - and it is *newer* than ``v0.21.3``, not older.
* a detached checkout of an old tag also declares a version, but nothing tracks it.

So the reported version is demoted to ``reported_version`` and the update
decision is made from git provenance instead:

    git provenance  ->  release tag  ->  hermes --version (last resort)

Channels
--------

======================  ==========================================================
 channel                 meaning
======================  ==========================================================
 ``STABLE``              HEAD is exactly a (non-prerelease) release tag
 ``MAIN``                branch ``main``/``master``, ahead of or level with a tag
 ``PRERELEASE``          HEAD at a prerelease tag / reported version is prerelease
 ``DETACHED``            detached HEAD that matches no known release tag
 ``CUSTOM``              some other branch/build we cannot map to a release
 ``UNKNOWN``             no usable git or version information at all
======================  ==========================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .github_api import CompareResult, Release
from .local_env import GitState, LocalEnv
from .overrides import run_git
from .versioning import compare_versions, normalise_tag, parse_version

CHANNEL_STABLE = "STABLE"
CHANNEL_MAIN = "MAIN"
CHANNEL_PRERELEASE = "PRERELEASE"
CHANNEL_DETACHED = "DETACHED"
CHANNEL_CUSTOM = "CUSTOM"
CHANNEL_UNKNOWN = "UNKNOWN"

#: Install-state classification (phase 4). Coarser than the channel and meant for
#: humans: what kind of installation is this, and does it need special handling?
INSTALL_STANDARD_RELEASE = "STANDARD_RELEASE"
INSTALL_MAIN_CLEAN = "MAIN_CLEAN"
INSTALL_MAIN_WITH_MANAGED_OVERRIDES = "MAIN_WITH_MANAGED_OVERRIDES"
INSTALL_CUSTOM_COMMIT = "CUSTOM_COMMIT"
INSTALL_FORK = "FORK"
INSTALL_DETACHED = "DETACHED"
INSTALL_UNKNOWN = "UNKNOWN"

#: where the ahead/behind numbers came from
COMPARE_SOURCE_GITHUB = "github"
COMPARE_SOURCE_LOCAL_GIT = "local_git"
COMPARE_SOURCE_NONE = "none"

ALL_CHANNELS = (
    CHANNEL_STABLE,
    CHANNEL_MAIN,
    CHANNEL_PRERELEASE,
    CHANNEL_DETACHED,
    CHANNEL_CUSTOM,
    CHANNEL_UNKNOWN,
)

DEV_BRANCHES = {"main", "master", "develop", "development", "dev"}

#: Update-availability outcomes (superset of the old boolean).
UPDATE_STATUS_UP_TO_DATE = "up_to_date"
UPDATE_STATUS_AVAILABLE = "update_available"
UPDATE_STATUS_AHEAD = "ahead_of_stable"
UPDATE_STATUS_MANUAL_REVIEW = "manual_review"
UPDATE_STATUS_UNKNOWN = "unknown"

CompareFn = Callable[[str, str], Optional[CompareResult]]


@dataclass
class CodeProvenance:
    """Where the running code actually sits relative to the release tags."""

    reported_version: Optional[str] = None
    reported_tag: Optional[str] = None
    channel: str = CHANNEL_UNKNOWN
    git_branch: Optional[str] = None
    git_commit: Optional[str] = None
    detached: bool = False
    nearest_tag: Optional[str] = None
    nearest_tag_version: Optional[str] = None
    commits_ahead_of_tag: Optional[int] = None
    commits_behind_target: Optional[int] = None
    dirty_worktree: bool = False
    dirty_files: int = 0
    tag_matched: bool = False
    compare_available: bool = False
    compare_source: str = COMPARE_SOURCE_NONE
    is_git_install: bool = False
    install_state: str = INSTALL_UNKNOWN
    remote_url: Optional[str] = None
    fork_detected: bool = False
    upstream_remote: Optional[str] = None
    local_only_commits: Optional[int] = None
    managed_overrides: int = 0
    unknown_changes: int = 0
    override_drifted: int = 0
    evidence: list[tuple[str, str]] = field(default_factory=list)

    # -- derived ------------------------------------------------------------- #

    @property
    def ahead_of_stable(self) -> bool:
        """HEAD contains commits that the latest stable release does not."""
        return bool(self.commits_ahead_of_tag and self.commits_ahead_of_tag > 0)

    @property
    def exact_release_tag(self) -> bool:
        return self.tag_matched

    @property
    def is_development_channel(self) -> bool:
        return self.channel == CHANNEL_MAIN

    def channel_note_zh(self) -> str:
        return {
            CHANNEL_STABLE: "运行在正式 Release tag 上",
            CHANNEL_MAIN: "跟踪开发分支（main）",
            CHANNEL_PRERELEASE: "运行在预发布版本上",
            CHANNEL_DETACHED: "detached HEAD，无法对应任何 Release tag",
            CHANNEL_CUSTOM: "自定义分支/构建，无法对应已知 Release",
            CHANNEL_UNKNOWN: "无法判断代码来源",
        }.get(self.channel, self.channel)

    def channel_note_en(self) -> str:
        return {
            CHANNEL_STABLE: "running an exact release tag",
            CHANNEL_MAIN: "tracking the development branch (main)",
            CHANNEL_PRERELEASE: "running a prerelease build",
            CHANNEL_DETACHED: "detached HEAD that matches no release tag",
            CHANNEL_CUSTOM: "custom branch/build that maps to no known release",
            CHANNEL_UNKNOWN: "code provenance unknown",
        }.get(self.channel, self.channel)

    def to_dict(self) -> dict[str, object]:
        """Includes the flat keys promised in the JSON contract."""
        return {
            # flat contract keys (documented in the README)
            "reported_version": self.reported_version,
            "channel": self.channel,
            "branch": self.git_branch,
            "commit": self.git_commit,
            "nearest_tag": self.nearest_tag,
            "ahead_by": self.commits_ahead_of_tag,
            "dirty": self.dirty_worktree,
            # extra detail
            "reported_tag": self.reported_tag,
            "detached": self.detached,
            "nearest_tag_version": self.nearest_tag_version,
            "behind_by": self.commits_behind_target,
            "tag_matched": self.tag_matched,
            "is_git_install": self.is_git_install,
            "compare_available": self.compare_available,
            "compare_source": self.compare_source,
            "install_state": self.install_state,
            "remote_url": self.remote_url,
            "fork_detected": self.fork_detected,
            "upstream_remote": self.upstream_remote,
            "local_only_commits": self.local_only_commits,
            "managed_overrides": self.managed_overrides,
            "unknown_changes": self.unknown_changes,
            "override_drifted": self.override_drifted,
            "dirty_files": self.dirty_files,
            "evidence": [{"zh": zh, "en": en} for zh, en in self.evidence],
        }


@dataclass
class UpdateDecision:
    """Is an update even the right question to ask?"""

    status: str = UPDATE_STATUS_UNKNOWN
    is_update_candidate: Optional[bool] = None
    target_version: Optional[str] = None
    target_tag: Optional[str] = None
    message_zh: str = ""
    message_en: str = ""

    @property
    def headline_zh(self) -> str:
        return {
            UPDATE_STATUS_UP_TO_DATE: "当前已是最新正式版本",
            UPDATE_STATUS_AVAILABLE: "存在可用的正式版本更新",
            UPDATE_STATUS_AHEAD: "当前代码已领先最新正式版本",
            UPDATE_STATUS_MANUAL_REVIEW: "需要人工判断（非标准安装状态）",
            UPDATE_STATUS_UNKNOWN: "无法判断更新状态",
        }.get(self.status, self.status)

    @property
    def headline_en(self) -> str:
        return {
            UPDATE_STATUS_UP_TO_DATE: "already on the latest stable release",
            UPDATE_STATUS_AVAILABLE: "a newer stable release exists",
            UPDATE_STATUS_AHEAD: "current code is ahead of the latest stable release",
            UPDATE_STATUS_MANUAL_REVIEW: "manual review required (non-standard install state)",
            UPDATE_STATUS_UNKNOWN: "update status unknown",
        }.get(self.status, self.status)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "is_update_candidate": self.is_update_candidate,
            "target_version": self.target_version,
            "target_tag": self.target_tag,
            "message_zh": self.message_zh,
            "message_en": self.message_en,
        }


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #


def resolve_provenance(
    env: LocalEnv,
    releases: Sequence[Release] = (),
    *,
    latest: Optional[Release] = None,
    compare: Optional[CompareFn] = None,
    target_commit: Optional[str] = None,
    runner: Optional[Any] = None,
    official_repo: str = "NousResearch/hermes-agent",
    managed_overrides: int = 0,
    unknown_changes: int = 0,
    drifted_overrides: int = 0,
) -> CodeProvenance:
    """Build the provenance model from the local environment plus GitHub data.

    ``compare`` is an optional ``compare(base, head) -> CompareResult|None``
    callable (the GitHub client's); when it is unavailable the model falls back to
    the *local* git (phase 4) instead of declaring the install unknown, and says
    which source produced the numbers (``compare_source``).
    """
    git: Optional[GitState] = env.git if env.git and env.git.is_repo else None
    prov = CodeProvenance(
        reported_version=env.version,
        reported_tag=normalise_tag(env.release_tag),
        is_git_install=bool(git),
    )

    if git is None:
        # Non-git installs (pip/docker/nix): the reported version is all we have.
        prov.channel = _channel_for_version(env.version, env.release_tag, releases)
        prov.reported_tag = normalise_tag(env.release_tag)
        if prov.channel == CHANNEL_STABLE:
            prov.nearest_tag = prov.reported_tag
            prov.tag_matched = True
        prov.evidence.append(
            (
                f"非 git 安装（{env.install_kind}）：代码来源只能依据 hermes --version 报告",
                f"non-git install ({env.install_kind}): provenance inferred from the reported version only",
            )
        )
        return prov

    prov.git_branch = git.branch
    prov.git_commit = git.commit
    prov.dirty_worktree = bool(git.dirty)
    prov.dirty_files = len(git.dirty_files)
    prov.detached = _is_detached(git)

    head_tags = [normalise_tag(tag) for tag in git.tags_at_head if tag]
    known_tags = {normalise_tag(release.tag) for release in releases if release.tag}
    # A HEAD tag counts as matched when it is a known release OR the tag the CLI
    # itself reported (the release list only covers the most recent 30 releases).
    prov.tag_matched = bool(head_tags) and any(
        tag in known_tags or (prov.reported_tag is not None and tag == prov.reported_tag) for tag in head_tags
    )

    # -- relation to the latest stable tag ---------------------------------- #
    target_tag = latest.tag if latest else None
    if compare is not None and target_tag:
        relation = compare(target_tag, git.full_commit or git.commit or target_tag)
        if relation is not None:
            prov.compare_available = True
            prov.compare_source = COMPARE_SOURCE_GITHUB
            if relation.status == "identical":
                prov.tag_matched = True
                prov.nearest_tag = normalise_tag(target_tag)
            elif relation.status == "ahead":
                prov.nearest_tag = normalise_tag(target_tag)
                prov.commits_ahead_of_tag = relation.ahead_by
            elif relation.status == "behind":
                prov.commits_behind_target = relation.behind_by
            elif relation.status == "diverged":
                prov.commits_ahead_of_tag = relation.ahead_by
                prov.commits_behind_target = relation.behind_by

    # -- local git fallback (phase 4) ---------------------------------------- #
    # GitHub compare can 404 when the local commit was never pushed, came from
    # another remote or the tag is not fetched. Local git answers the same
    # question, so the install is *not* unknown just because GitHub is.
    if not prov.compare_available and target_tag:
        local = local_git_relation(env.install_dir, target_tag, runner=runner)
        local_ref = target_tag
        if local is None and target_commit:
            local = local_git_relation(env.install_dir, target_commit, runner=runner)
            local_ref = target_commit[:12]
        if local is not None:
            behind, ahead = local
            prov.compare_available = True
            prov.compare_source = COMPARE_SOURCE_LOCAL_GIT
            if ahead and not behind:
                prov.nearest_tag = normalise_tag(target_tag)
                prov.commits_ahead_of_tag = ahead
            elif behind and not ahead:
                prov.commits_behind_target = behind
            elif ahead and behind:
                prov.commits_ahead_of_tag = ahead
                prov.commits_behind_target = behind
            prov.evidence.append(
                (
                    f"GitHub compare 不可用，改用本机 git 计算（对照 {local_ref}）：领先 {ahead} / 落后 {behind}",
                    f"GitHub compare unavailable, computed with local git (against {local_ref}): {ahead} ahead / {behind} behind",
                )
            )
        else:
            upstream = git.upstream_commit or local_git_relation(env.install_dir, "origin/main", runner=runner)
            origin_rel = local_git_relation(env.install_dir, "origin/main", runner=runner)
            if origin_rel is not None:
                behind_main, ahead_main = origin_rel
                prov.local_only_commits = ahead_main
                prov.evidence.append(
                    (
                        f"与本机记录的 origin/main 相比：领先 {ahead_main} / 落后 {behind_main}（发布 tag 不在本地对象库）",
                        f"vs the locally recorded origin/main: {ahead_main} ahead / {behind_main} behind (release tag not in the local object store)",
                    )
                )
            elif upstream:
                prov.evidence.append(("本机 git 无法给出与上游的关系", "local git cannot relate HEAD to upstream"))
            prov.evidence.append(
                (
                    "GitHub compare 不可用，且本机 git 也算不出距离：不据此断定安装异常",
                    "GitHub compare unavailable and local git cannot measure the distance either: this alone is not an install anomaly",
                )
            )
    if prov.nearest_tag is None and prov.tag_matched and head_tags:
        prov.nearest_tag = head_tags[0]
    if prov.nearest_tag is None and prov.reported_tag:
        # Fall back to what the CLI told us; mark it as less certain.
        prov.nearest_tag = prov.reported_tag

    if prov.nearest_tag:
        match = next((r for r in releases if normalise_tag(r.tag) == prov.nearest_tag), None)
        prov.nearest_tag_version = match.display_version if match else None

    # -- channel ------------------------------------------------------------ #
    prov.channel = _classify_channel(prov, git, env)

    # -- install state (phase 4) --------------------------------------------- #
    prov.remote_url = git.remote_url
    prov.fork_detected = _looks_like_fork(git.remote_url, official_repo)
    prov.managed_overrides = managed_overrides
    prov.unknown_changes = unknown_changes
    prov.override_drifted = drifted_overrides
    prov.install_state = _install_state(prov, git, managed_overrides, unknown_changes)
    if prov.fork_detected:
        prov.evidence.append(
            (
                f"origin 不是官方仓库（{git.remote_url}）：按 fork 场景处理，不用官方 compare 判定异常",
                f"origin is not the official repository ({git.remote_url}): treated as a fork, "
                "the official compare is not used to call it anomalous",
            )
        )
    if prov.local_only_commits:
        prov.evidence.append(
            (
                f"本地有 {prov.local_only_commits} 个未推送的提交（GitHub 上不存在，属正常本地定制）",
                f"{prov.local_only_commits} local-only commit(s) that GitHub does not have (normal local customization)",
            )
        )

    if prov.detached:
        prov.evidence.append(
            (
                "HEAD 处于 detached 状态",
                "HEAD is detached",
            )
        )
    if prov.ahead_of_stable:
        prov.evidence.append(
            (
                f"本地 HEAD 领先 {prov.nearest_tag} {prov.commits_ahead_of_tag} 个 commit",
                f"local HEAD is {prov.commits_ahead_of_tag} commits ahead of {prov.nearest_tag}",
            )
        )
    if prov.commits_behind_target:
        prov.evidence.append(
            (
                f"本地 HEAD 落后 {target_tag} {prov.commits_behind_target} 个 commit",
                f"local HEAD is {prov.commits_behind_target} commits behind {target_tag}",
            )
        )
    if prov.channel != CHANNEL_UNKNOWN and not prov.compare_available:
        prov.evidence.append(
            (
                "GitHub compare 不可用，本机 git 也算不出与发布版本的距离：不据此断定安装异常",
                "GitHub compare unavailable and local git cannot measure the distance to the release either: "
                "this alone is not an install anomaly",
            )
        )
    if prov.reported_tag and prov.nearest_tag and prov.reported_tag != prov.nearest_tag:
        prov.evidence.append(
            (
                f"hermes --version 报告 {prov.reported_tag}，但代码位置更接近 {prov.nearest_tag}",
                f"hermes --version reports {prov.reported_tag} while the code sits nearer {prov.nearest_tag}",
            )
        )
    return prov


def local_git_relation(
    install_dir: Optional[Path], ref: str, *, runner: Optional[Any] = None
) -> Optional[tuple[int, int]]:
    """(behind, ahead) between ``ref`` and HEAD, computed with the local git only.

    Local git is authoritative for the local repository: a GitHub compare that
    returns 404 (the commit was never pushed, comes from another remote, or the
    tag is not fetched) must not turn the install into "unknown".
    """
    if install_dir is None or not ref:
        return None
    result = run_git(install_dir, ["rev-list", "--left-right", "--count", f"{ref}...HEAD"], runner=runner)
    if result.returncode != 0:
        return None
    parts = (result.stdout or "").split()
    if len(parts) != 2:
        return None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return behind, ahead


def _looks_like_fork(remote_url: Optional[str], official_repo: str) -> bool:
    """A fork's origin is not the official repository (upstream usually is)."""
    if not remote_url:
        return False
    slug = remote_url.rstrip("/").removesuffix(".git")
    slug = slug.rsplit(":", 1)[-1] if "://" not in slug else slug
    parts = slug.split("/")
    if len(parts) < 2:
        return False
    return f"{parts[-2]}/{parts[-1]}".lower() != official_repo.lower()


def _is_detached(git: GitState) -> bool:
    branch = (git.branch or "").strip()
    return branch in {"", "HEAD", "(no branch)"} or branch.startswith("(HEAD detached")


def _install_state(prov: CodeProvenance, git: GitState, managed: int, unknown: int) -> str:
    """Classify the installation shape (phase 4).

    This is deliberately coarse and never *by itself* means "review manually":
    a customized checkout is a normal, supported state.
    """
    if not prov.is_git_install:
        return INSTALL_STANDARD_RELEASE if prov.channel == CHANNEL_STABLE else INSTALL_UNKNOWN
    if prov.fork_detected:
        return INSTALL_FORK
    if prov.detached or prov.channel == CHANNEL_DETACHED:
        return INSTALL_DETACHED
    if prov.channel == CHANNEL_STABLE or prov.channel == CHANNEL_PRERELEASE:
        return INSTALL_STANDARD_RELEASE
    if prov.channel == CHANNEL_MAIN:
        if managed or prov.local_only_commits:
            return INSTALL_MAIN_WITH_MANAGED_OVERRIDES
        if unknown or prov.dirty_worktree:
            return INSTALL_MAIN_CLEAN
        return INSTALL_MAIN_CLEAN
    if prov.channel == CHANNEL_CUSTOM:
        return INSTALL_CUSTOM_COMMIT
    return INSTALL_UNKNOWN


#: The only conditions that justify MANUAL_REVIEW (phase 4, doc section 36).
MANUAL_REVIEW_REASONS: dict[str, tuple[str, str]] = {
    "unknown_repo": ("无法识别这个仓库（不是 git 安装，也不是已知发行版）", "the repository cannot be identified"),
    "override_drift": ("本地定制在登记之后又被改过（drift）", "a registered override drifted since registration"),
    "unknown_dirty": ("工作区有未登记的修改", "the working tree has unregistered changes"),
    "conflict_low": (
        "预测到本地定制与新版本冲突（需要人工合并）",
        "a local override is predicted to conflict (manual merge needed)",
    ),
    "git_graph": ("本机 git 历史无法解析", "the local git history cannot be parsed"),
    "interrupted_transaction": ("上次更新事务中断，尚未收尾", "the previous update transaction was interrupted"),
}


def _classify_channel(prov: CodeProvenance, git: GitState, env: LocalEnv) -> str:
    """Channel precedence: prerelease -> tag/stable -> detached -> dev branch -> custom."""
    # A prerelease build is a prerelease build, even when it sits on a tag:
    # the reported version is the strongest signal about the running code.
    if _looks_prerelease(env.version, prov.reported_tag):
        return CHANNEL_PRERELEASE

    if prov.tag_matched and prov.nearest_tag:
        return CHANNEL_STABLE

    if prov.detached:
        return CHANNEL_DETACHED

    branch = (git.branch or "").strip()
    if branch.lower() in DEV_BRANCHES:
        return CHANNEL_MAIN

    if not branch and not git.commit:
        return CHANNEL_UNKNOWN
    return CHANNEL_CUSTOM


def _channel_for_version(version: Optional[str], tag: Optional[str], releases: Sequence[Release]) -> str:
    if not version and not tag:
        return CHANNEL_UNKNOWN
    normalised = normalise_tag(tag)
    if normalised and _tag_is_prerelease(normalised):
        return CHANNEL_PRERELEASE
    parsed = parse_version(version)
    if parsed is not None and parsed.is_prerelease:
        return CHANNEL_PRERELEASE
    for release in releases:
        if release.display_version and version and release.display_version == version:
            return CHANNEL_STABLE
        if normalised and normalise_tag(release.tag) == normalised:
            return CHANNEL_STABLE
    return CHANNEL_CUSTOM


def _tag_is_prerelease(tag: str) -> bool:
    parsed = parse_version(_version_from_tag(tag))
    return bool(parsed and parsed.is_prerelease)


def _version_from_tag(tag: str) -> Optional[str]:
    """Semantic version behind a tag - or None for date-style tags.

    ``v2026.9.14`` is a *date*, not version 2026.9.14; treating it as a semver
    would make every comparison nonsense (2026.9.14 > 0.21.3).
    """
    text = tag.lstrip("v")
    parsed_version = parse_version(text)
    if parsed_version is not None and parsed_version.major >= 1000:
        return None
    if _looks_like_date_tag(text):
        return None
    return text


def _looks_like_date_tag(text: str) -> bool:
    parts = text.split(".")
    if len(parts) not in (3, 4):
        return False
    try:
        head = int(parts[0])
    except ValueError:
        return False
    return 1900 <= head <= 2100 and len(parts[0]) == 4


def _looks_prerelease(version: Optional[str], tag: Optional[str]) -> bool:
    for value in (version, _version_from_tag(tag) if tag else None):
        if not value:
            continue
        parsed = parse_version(value)
        if parsed is not None and parsed.is_prerelease:
            return True
    return False


# --------------------------------------------------------------------------- #
# update decision
# --------------------------------------------------------------------------- #


def decide_update(
    prov: CodeProvenance,
    latest: Optional[Release],
    *,
    preferred_channel: str = "stable",
    allow_prerelease: bool = False,
) -> UpdateDecision:
    """Decide *whether* a newer release is the right thing to move to.

    A checkout that already contains the latest release tag plus commits is not
    an update candidate - "updating" would move it *backwards*.
    """
    decision = UpdateDecision()
    if latest is None:
        decision.status = UPDATE_STATUS_UNKNOWN
        decision.message_zh = "没有可用的 Release 信息"
        decision.message_en = "no release information available"
        return decision

    decision.target_version = latest.display_version
    decision.target_tag = latest.tag
    target_label = latest.tag or (latest.display_version or "the latest release")

    if prov.channel == CHANNEL_UNKNOWN:
        decision.status = UPDATE_STATUS_UNKNOWN
        decision.message_zh = "无法判断代码来源，请人工确认后再决定是否更新"
        decision.message_en = "code provenance unknown; review manually before updating"
        return decision

    if prov.channel in {CHANNEL_MAIN, CHANNEL_CUSTOM, CHANNEL_DETACHED}:
        if prov.ahead_of_stable and not prov.commits_behind_target:
            decision.status = UPDATE_STATUS_AHEAD
            decision.is_update_candidate = False
            decision.message_zh = (
                "当前安装跟踪开发分支（main），并且已经领先最新正式版本 "
                f"{prov.commits_ahead_of_tag} 个 commit。"
                "把代码切回正式 Release 不是“升级”，而是一次（可能的）降级，请先备份再人工决定。"
            )
            decision.message_en = (
                "Current installation is tracking development/main and is already ahead of the latest "
                f"stable release by {prov.commits_ahead_of_tag} commits. "
                "Moving to the tagged release is not an upgrade - it is a (possible) downgrade; back up first."
            )
            return decision
        if prov.channel == CHANNEL_MAIN and prov.commits_behind_target and not prov.ahead_of_stable:
            # On a dev branch but verifiably *behind* the release: pulling does
            # move forward. It is still not a normal upgrade path (the branch,
            # not the tag, decides what the code becomes) - the main-branch gate
            # says so explicitly.
            decision.status = UPDATE_STATUS_AVAILABLE
            decision.is_update_candidate = True
            decision.message_zh = (
                f"当前在 main 分支且落后最新正式版本 {prov.commits_behind_target} 个 commit："
                "可以同步代码，但注意更新后拿到的是 main 而不是该 Release。"
            )
            decision.message_en = (
                f"on the main branch and {prov.commits_behind_target} commits behind the latest stable release: "
                "the code can be brought forward, but you will end up on main, not on the tagged release."
            )
            return decision
        if prov.channel == CHANNEL_MAIN and prov.ahead_of_stable and prov.commits_behind_target:
            # Phase 4 (doc 29/34/36): main + local commits + upstream commits it does
            # not have is the *normal* shape of a customized main-tracking checkout -
            # `hermes update` brings the upstream work in and keeps the local commits.
            # It is not "an abnormal installation state", so not MANUAL_REVIEW.
            decision.status = UPDATE_STATUS_AVAILABLE
            decision.is_update_candidate = True
            local_note = ""
            if prov.local_only_commits:
                local_note = f"；其中 {prov.local_only_commits} 个提交未推送到远端（属正常本地定制）"
            decision.message_zh = (
                f"main 分支与 {target_label} 各有提交：本地领先 {prov.commits_ahead_of_tag} 个，"
                f"落后 {prov.commits_behind_target} 个。更新会把上游的 {prov.commits_behind_target} 个提交合进来，"
                "你的本地提交保留；结果是 main 而不是该 Release" + local_note + "。"
            )
            decision.message_en = (
                f"main and {target_label} have each moved: {prov.commits_ahead_of_tag} local commit(s) ahead, "
                f"{prov.commits_behind_target} behind. Updating merges the upstream commits in and keeps the "
                "local ones; the result is main, not the tagged release."
            )
            return decision
        if prov.channel == CHANNEL_MAIN and not prov.compare_available:
            # Phase 4: a main-tracking checkout whose distance to the release cannot
            # be measured (GitHub compare 404, tag not fetched locally) is *not* a
            # manual-review case. The release exists and the code does not contain
            # it; whether the local commits are ahead is unproven, so say that.
            decision.status = UPDATE_STATUS_AVAILABLE
            decision.is_update_candidate = True
            local_note = ""
            if prov.local_only_commits:
                local_note = f"；本地有 {prov.local_only_commits} 个未推送提交（属正常本地定制）"
            decision.message_zh = (
                "存在可用的正式版本更新。与发布版本的距离无法精确计算"
                "（GitHub compare 与本地 tag 都没给出），但可以同步 main；"
                "更新后拿到的是 main 而不是该 Release" + local_note + "。"
            )
            decision.message_en = (
                "a newer stable release exists. The exact distance could not be measured "
                "(neither GitHub compare nor a local tag answered), but main can be brought forward; "
                "the result is main, not the tagged release."
            )
            return decision
        decision.status = UPDATE_STATUS_MANUAL_REVIEW
        decision.is_update_candidate = False
        if prov.channel == CHANNEL_DETACHED:
            decision.message_zh = "HEAD 处于 detached 状态且无法对应任何 Release tag：请人工确认代码来源。"
            decision.message_en = "HEAD is detached and matches no release tag: confirm provenance manually."
        else:
            decision.message_zh = "当前分支/构建不是标准 Release 状态：请人工确认后再决定是否更新。"
            decision.message_en = "the current branch/build is not a standard release state: review manually."
        return decision
    if prov.channel == CHANNEL_PRERELEASE:
        decision.status = UPDATE_STATUS_MANUAL_REVIEW
        decision.is_update_candidate = bool(allow_prerelease)
        decision.message_zh = "当前运行的是预发布版本：默认不建议跟随更新。"
        decision.message_en = "running a prerelease build: following updates is not recommended by default."
        return decision

    # -- STABLE (by tag) or a version-matched non-git install ---------------- #
    current = _current_comparable_version(prov)
    if current is None or not latest.display_version:
        decision.status = UPDATE_STATUS_UNKNOWN
        decision.message_zh = "无法比较当前版本与最新版本"
        decision.message_en = "cannot compare the current and latest versions"
        return decision

    if prov.commits_behind_target and prov.commits_behind_target > 0:
        decision.status = UPDATE_STATUS_AVAILABLE
        decision.is_update_candidate = True
        decision.message_zh = f"当前代码落后最新正式版本 {prov.commits_behind_target} 个 commit"
        decision.message_en = f"current code is {prov.commits_behind_target} commits behind the latest stable release"
        return decision

    comparison = compare_versions(latest.display_version, current)
    if comparison > 0:
        decision.status = UPDATE_STATUS_AVAILABLE
        decision.is_update_candidate = True
        decision.message_zh = f"存在更新的正式版本：{current} -> {latest.display_version}"
        decision.message_en = f"a newer stable release exists: {current} -> {latest.display_version}"
    elif comparison == 0:
        decision.status = UPDATE_STATUS_UP_TO_DATE
        decision.is_update_candidate = False
        decision.message_zh = "当前已是最新正式版本"
        decision.message_en = "already on the latest stable release"
    else:
        decision.status = UPDATE_STATUS_MANUAL_REVIEW
        decision.is_update_candidate = False
        decision.message_zh = "当前版本号高于最新 Release（可能来自自定义构建）：请人工确认"
        decision.message_en = "current version is higher than the newest release (custom build?): review manually"
    return decision


def _current_comparable_version(prov: CodeProvenance) -> Optional[str]:
    """The semver of the running code, for release-to-release comparison.

    Date-style tags cannot be compared numerically, so they are skipped and the
    version reported by ``hermes --version`` is used instead.
    """
    if prov.tag_matched and prov.nearest_tag:
        version = prov.nearest_tag_version or _version_from_tag(prov.nearest_tag)
        if version and prov.reported_version and prov.nearest_tag_version is None:
            # we know the tag but not the semver behind it: trust the report
            return prov.reported_version or version
        if version:
            return version
    if prov.reported_version:
        return prov.reported_version
    if prov.nearest_tag:
        return prov.nearest_tag_version or _version_from_tag(prov.nearest_tag)
    return None


def channel_mismatch(prov: CodeProvenance, preferred_channel: str) -> Optional[tuple[str, str]]:
    """Warning text when the install does not track the preferred channel."""
    preferred = (preferred_channel or "stable").lower()
    actual = prov.channel
    if preferred == "stable" and actual in {CHANNEL_MAIN, CHANNEL_CUSTOM, CHANNEL_DETACHED}:
        return (
            f"当前跟踪的是 {actual}，而 preferred_channel 是 stable："
            "建议备份后切换到正式 Release（本工具不会自动降级）",
            f"You are currently tracking {actual} while preferred_channel is stable. "
            "Consider switching to a stable release after backing up (this tool never downgrades automatically).",
        )
    if preferred == "main" and actual == CHANNEL_STABLE:
        return (
            "当前运行在正式 Release，而 preferred_channel 是 main",
            "You are on a stable release while preferred_channel is main.",
        )
    if preferred == "prerelease" and actual == CHANNEL_STABLE:
        return (
            "当前运行在正式 Release，而 preferred_channel 是 prerelease",
            "You are on a stable release while preferred_channel is prerelease.",
        )
    return None


def now_version_age_days(published: Optional[datetime], *, now: Optional[datetime] = None) -> Optional[float]:
    from .util import hours_between, utcnow

    if published is None:
        return None
    return hours_between(published, now or utcnow()) / 24.0
