"""Managed local overrides: registry, classification, patches, prediction, apply.

Most tests run against a *real* temporary git repository (git is the thing being
integrated with - stubbing it would test the stub). Tests that need git skip when
no git executable is on PATH, matching the rest of the suite; the pure parsing and
registry tests always run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from hermes_update_check import overrides as ov
from hermes_update_check.util import ProcResult

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git executable not available")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout


def _commit(repo: Path, message: str = "commit") -> str:
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A tiny git repo with one initial commit."""
    if GIT is None:  # pragma: no cover - guarded by needs_git
        pytest.skip("git executable not available")
    root = tmp_path / "install"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "hermes").mkdir()
    (root / "hermes" / "a.py").write_text("print('a')\nline2\nline3\nline4\n", encoding="utf-8")
    (root / "hermes" / "b.py").write_text("print('b')\n", encoding="utf-8")
    (root / "desktop").mkdir()
    (root / "desktop" / "foo.ts").write_text("export const foo = 1;\n", encoding="utf-8")
    (root / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    _git(root, "add", "-A")
    _commit(root, "initial")
    return root


@pytest.fixture()
def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir()
    return root


def _modify(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# pure parsing / registry tests (no git needed)
# --------------------------------------------------------------------------- #


def test_parse_porcelain_all_statuses() -> None:
    text = (
        " M package-lock.json\n"
        "A  hermes/new.py\n"
        "D  desktop/foo.ts\n"
        "R  hermes/old.py -> hermes/newer.py\n"
        "?? scratch.txt\n"
        "!! build/out.bin\n"
        "MM hermes/both.py\n"
    )
    changes = {change.path: change for change in ov.parse_porcelain(text)}
    assert changes["package-lock.json"].status == ov.STATUS_MODIFIED
    assert changes["hermes/new.py"].status == ov.STATUS_ADDED
    assert changes["desktop/foo.ts"].status == ov.STATUS_DELETED
    assert changes["hermes/newer.py"].status == ov.STATUS_RENAMED
    assert changes["hermes/newer.py"].renamed_from == "hermes/old.py"
    assert changes["scratch.txt"].untracked is True
    assert changes["build/out.bin"].ignored is True
    assert changes["hermes/both.py"].status == ov.STATUS_MODIFIED


def test_registry_round_trip(tmp_path: Path) -> None:
    registry = ov.OverrideRegistry(
        base_commit="abc1234",
        base_branch="main",
        base_release="v2026.9.14",
        registered_at="2026-09-18T00:00:00Z",
        files=[ov.OverrideEntry(path="hermes/a.py", status=ov.STATUS_MODIFIED, current_sha256="deadbeef")],
    )
    root = tmp_path / "state"
    ov.save_registry(root, registry)
    loaded = ov.load_registry(root)
    assert loaded.base_commit == "abc1234"
    assert loaded.files[0].path == "hermes/a.py"
    assert loaded.files[0].current_sha256 == "deadbeef"
    assert loaded.broken is False


def test_corrupted_registry_is_reported_not_raised(tmp_path: Path) -> None:
    root = tmp_path / "state"
    ov.ensure_dir(ov.overrides_root(root))
    ov.registry_path(root).write_text("{not json", encoding="utf-8")
    registry = ov.load_registry(root)
    assert registry.broken is True
    assert "unreadable" in registry.broken_reason or "JSON" in registry.broken_reason
    # a future registry version is not silently mis-read either
    ov.registry_path(root).write_text(json.dumps({"version": 99, "files": []}), encoding="utf-8")
    assert ov.load_registry(root).broken is True


def test_hunk_ranges_and_overlap() -> None:
    diff = (
        "diff --git a/hermes/a.py b/hermes/a.py\n"
        "--- a/hermes/a.py\n"
        "+++ b/hermes/a.py\n"
        "@@ -1,3 +1,4 @@\n"
        "+new\n"
        " line2\n"
        " line3\n"
        " line4\n"
    )
    ranges = ov._hunk_ranges(diff)
    assert ranges["hermes/a.py"] == [(1, 3)]
    assert ov._overlaps([(1, 3)], [(3, 6)]) is True
    assert ov._overlaps([(1, 3)], [(10, 12)]) is False


def test_sanitise_keeps_snapshot_paths_tame() -> None:
    assert "/" not in ov._sanitise("hermes/sub/dir/a.py").replace("_", "")
    assert ov._sanitise("") == "entry"
    assert ov._sanitise("a b c.py") == "a_b_c.py"


# --------------------------------------------------------------------------- #
# detection (real repo)
# --------------------------------------------------------------------------- #


@needs_git
def test_detect_lists_tracked_untracked_and_ignored(repo: Path) -> None:
    _modify(repo, "hermes/a.py", "changed\n")
    (repo / "scratch.txt").write_text("note\n", encoding="utf-8")
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_bytes(b"\x00\x01")
    changes = ov.detect_changes(repo)
    tracked, untracked, ignored = ov.split_changes(changes)
    assert [c.path for c in tracked] == ["hermes/a.py"]
    assert "scratch.txt" in [c.path for c in untracked]
    assert any(path.startswith("build/") for path in ignored)


@needs_git
def test_clean_repo_has_no_changes(repo: Path) -> None:
    assert ov.detect_changes(repo) == []


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #


@needs_git
def test_register_records_real_baseline(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "print('a')\nline2 CHANGED\nline3\nline4\n")
    head = _git(repo, "rev-parse", "HEAD").strip()
    result = ov.register_overrides(repo, state_root, base_release="v2026.9.14")
    assert result.registered == ["hermes/a.py"]
    registry = ov.load_registry(state_root)
    entry = registry.files[0]
    assert entry.base_commit == head
    assert entry.base_sha256 == ov.blob_sha256(repo, "hermes/a.py", head)
    assert entry.current_sha256 == ov.working_tree_sha256(repo, "hermes/a.py")
    assert entry.base_sha256 != entry.current_sha256
    assert entry.patch_sha256
    patch = Path(entry.patch_file)
    assert patch.is_file()
    assert "line2 CHANGED" in patch.read_text(encoding="utf-8")
    assert ov.sha256_bytes(patch.read_text(encoding="utf-8").encode("utf-8")) == entry.patch_sha256
    assert registry.base_commit == head
    assert registry.base_release == "v2026.9.14"


@needs_git
def test_register_refuses_untracked_by_default(repo: Path, state_root: Path) -> None:
    (repo / "scratch.txt").write_text("x\n", encoding="utf-8")
    result = ov.register_overrides(repo, state_root)
    assert result.registered == []
    assert "scratch.txt" not in ov.load_registry(state_root).paths()


@needs_git
def test_register_untracked_only_with_explicit_request(repo: Path, state_root: Path) -> None:
    (repo / "scratch.txt").write_text("x\n", encoding="utf-8")
    result = ov.register_overrides(repo, state_root, paths=["scratch.txt"], include_untracked=True)
    assert result.registered == ["scratch.txt"]
    entry = ov.load_registry(state_root).entry("scratch.txt")
    assert entry is not None
    assert entry.snapshot_file and Path(entry.snapshot_file).is_file()
    assert entry.current_sha256 == ov.working_tree_sha256(repo, "scratch.txt")


@needs_git
def test_register_twice_refreshes(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "changed once\n")
    ov.register_overrides(repo, state_root)
    _modify(repo, "hermes/a.py", "changed twice\n")
    result = ov.register_overrides(repo, state_root)
    assert result.refreshed == ["hermes/a.py"]
    assert result.registered == []
    registry = ov.load_registry(state_root)
    assert registry.files[0].current_sha256 == ov.working_tree_sha256(repo, "hermes/a.py")


# --------------------------------------------------------------------------- #
# classification: exact / drifted / missing / unknown
# --------------------------------------------------------------------------- #


@needs_git
def test_classify_exact_after_register(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "customized\n")
    ov.register_overrides(repo, state_root)
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.safety == ov.SAFETY_PASS
    assert report.managed_count == 1
    assert report.unknown_count == 0
    assert report.drifted_count == 0
    assert report.blocks_update is False


@needs_git
def test_classify_drift_when_edited_again(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "custom v1\n")
    ov.register_overrides(repo, state_root)
    _modify(repo, "hermes/a.py", "custom v2\n")
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.safety == ov.SAFETY_WARN
    assert report.drifted == ["hermes/a.py"]
    assert report.drifted_count == 1


@needs_git
def test_classify_missing_when_reverted(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "custom\n")
    ov.register_overrides(repo, state_root)
    _git(repo, "checkout", "--", "hermes/a.py")
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.missing == ["hermes/a.py"]
    assert report.safety == ov.SAFETY_WARN


@needs_git
def test_classify_unknown_for_unregistered_change(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "custom\n")
    ov.register_overrides(repo, state_root)
    _modify(repo, "hermes/b.py", "surprise\n")
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.safety == ov.SAFETY_FAIL
    assert [c.path for c in report.unknown] == ["hermes/b.py"]
    assert report.blocks_update is True


@needs_git
def test_classify_unknown_untracked_file(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "custom\n")
    ov.register_overrides(repo, state_root)
    (repo / "leftover.txt").write_text("hmm\n", encoding="utf-8")
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.safety == ov.SAFETY_FAIL
    assert report.unknown_count == 1


@needs_git
def test_classify_ignored_files_never_block(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "custom\n")
    ov.register_overrides(repo, state_root)
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_bytes(b"\x00")
    (repo / "debug.log").write_text("log\n", encoding="utf-8")
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.safety == ov.SAFETY_PASS
    assert report.unknown_count == 0
    assert report.ignored


@needs_git
def test_managed_deleted_file(repo: Path, state_root: Path) -> None:
    (repo / "desktop" / "foo.ts").unlink()
    ov.register_overrides(repo, state_root, paths=["desktop/foo.ts"])
    entry = ov.load_registry(state_root).entry("desktop/foo.ts")
    assert entry is not None and entry.status == ov.STATUS_DELETED
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.safety == ov.SAFETY_PASS
    assert report.managed_exact == ["desktop/foo.ts"]
    # the file coming back is drift, not silence
    _modify(repo, "desktop/foo.ts", "export const foo = 2;\n")
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.drifted == ["desktop/foo.ts"]


@needs_git
def test_managed_added_file(repo: Path, state_root: Path) -> None:
    (repo / "hermes" / "extra.py").write_text("extra\n", encoding="utf-8")
    _git(repo, "add", "hermes/extra.py")
    result = ov.register_overrides(repo, state_root, paths=["hermes/extra.py"])
    assert result.registered == ["hermes/extra.py"]
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.safety == ov.SAFETY_PASS


@needs_git
def test_managed_renamed_file(repo: Path, state_root: Path) -> None:
    _git(repo, "mv", "hermes/b.py", "hermes/b_renamed.py")
    ov.register_overrides(repo, state_root)
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.safety == ov.SAFETY_PASS
    assert any(path.endswith("b_renamed.py") for path in report.managed_exact)


@needs_git
def test_unknown_dirty_makes_status_fail(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "custom\n")
    _modify(repo, "hermes/b.py", "unknown\n")
    ov.register_overrides(repo, state_root, paths=["hermes/a.py"])
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.safety == ov.SAFETY_FAIL
    assert report.managed_count == 1 and report.unknown_count == 1


# --------------------------------------------------------------------------- #
# paths / encodings that break naive implementations
# --------------------------------------------------------------------------- #


@needs_git
def test_unicode_filename_and_spaces(repo: Path, state_root: Path) -> None:
    relative = "hermes/带 空格 的 文件.py"
    _modify(repo, relative, "unicode\n")
    _git(repo, "add", "-A")
    _commit(repo, "add unicode file")
    _modify(repo, relative, "unicode customized\n")
    result = ov.register_overrides(repo, state_root)
    assert result.registered == [relative]
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.safety == ov.SAFETY_PASS


@needs_git
def test_crlf_content_patch_round_trip(repo: Path, state_root: Path) -> None:
    path = repo / "hermes" / "crlf.py"
    path.write_bytes(b"line1\r\nline2\r\n")
    _git(repo, "add", "hermes/crlf.py")
    path.write_bytes(b"line1\r\nline2 CHANGED\r\n")
    ov.register_overrides(repo, state_root, paths=["hermes/crlf.py"])
    entry = ov.load_registry(state_root).entry("hermes/crlf.py")
    assert entry is not None
    patch_text = Path(entry.patch_file).read_text(encoding="utf-8")
    assert "line2 CHANGED" in patch_text
    assert ov.sha256_bytes(patch_text.encode("utf-8")) == entry.patch_sha256


@needs_git
def test_binary_file_patch_is_binary(repo: Path, state_root: Path) -> None:
    blob = repo / "hermes" / "logo.bin"
    blob.write_bytes(bytes(range(256)))
    _git(repo, "add", "hermes/logo.bin")
    _commit(repo, "add binary")
    blob.write_bytes(bytes(range(255, -1, -1)))
    ov.register_overrides(repo, state_root, paths=["hermes/logo.bin"])
    entry = ov.load_registry(state_root).entry("hermes/logo.bin")
    assert entry is not None
    text = Path(entry.patch_file).read_text(encoding="utf-8")
    assert "GIT binary patch" in text
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.safety == ov.SAFETY_PASS


# --------------------------------------------------------------------------- #
# prediction
# --------------------------------------------------------------------------- #


@needs_git
def test_predict_high_when_upstream_untouched(repo: Path, state_root: Path, tmp_path: Path) -> None:
    _modify(repo, "hermes/a.py", "custom\n")
    ov.register_overrides(repo, state_root)
    base = _git(repo, "rev-parse", "HEAD").strip()
    _modify(repo, "desktop/foo.ts", "export const foo = 2;\n")
    _git(repo, "add", "desktop/foo.ts")
    target = _commit(repo, "touch another file")
    prediction = ov.predict_reapply(repo, ov.load_registry(state_root), target)
    assert prediction.confidence == ov.CONFIDENCE_HIGH
    assert prediction.per_file["hermes/a.py"] == ov.CONFIDENCE_HIGH
    assert base  # sanity: the base was recorded


@needs_git
def test_predict_medium_when_upstream_touches_other_region(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "print('a')\nline2\nline3\nline4\n# local tail\n")
    ov.register_overrides(repo, state_root)
    base = _git(repo, "rev-parse", "HEAD").strip()
    _modify(repo, "hermes/a.py", "print('UPSTREAM')\nline2\nline3\nline4\n")
    _git(repo, "add", "hermes/a.py")
    target = _commit(repo, "upstream change to line 1")
    # the local customization is still uncommitted: restore it on top of the new target
    _git(repo, "checkout", "-q", base, "--", "hermes/a.py")
    _modify(repo, "hermes/a.py", "print('a')\nline2\nline3\nline4\n# local tail\n")
    prediction = ov.predict_reapply(repo, ov.load_registry(state_root), target)
    assert prediction.per_file["hermes/a.py"] in {ov.CONFIDENCE_MEDIUM, ov.CONFIDENCE_LOW}
    assert prediction.confidence in {ov.CONFIDENCE_MEDIUM, ov.CONFIDENCE_LOW}


@needs_git
def test_predict_low_on_overlapping_region(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "print('a')\nline2 LOCAL\nline3\nline4\n")
    ov.register_overrides(repo, state_root)
    base = _git(repo, "rev-parse", "HEAD").strip()
    _modify(repo, "hermes/a.py", "print('a')\nline2 UPSTREAM\nline3\nline4\n")
    _git(repo, "add", "hermes/a.py")
    target = _commit(repo, "upstream change to the same line")
    _git(repo, "checkout", "-q", base, "--", "hermes/a.py")
    _modify(repo, "hermes/a.py", "print('a')\nline2 LOCAL\nline3\nline4\n")
    prediction = ov.predict_reapply(repo, ov.load_registry(state_root), target)
    assert prediction.confidence == ov.CONFIDENCE_LOW
    assert "hermes/a.py" in prediction.conflicts
    assert prediction.manual_merge_likely is True


@needs_git
def test_predict_unknown_without_target(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "custom\n")
    ov.register_overrides(repo, state_root)
    prediction = ov.predict_reapply(repo, ov.load_registry(state_root), None)
    assert prediction.confidence == ov.CONFIDENCE_UNKNOWN


@needs_git
def test_predict_unknown_without_registry(repo: Path, state_root: Path) -> None:
    prediction = ov.predict_reapply(repo, ov.OverrideRegistry(), "HEAD")
    assert prediction.confidence == ov.CONFIDENCE_UNKNOWN


# --------------------------------------------------------------------------- #
# apply / restore
# --------------------------------------------------------------------------- #


@needs_git
def test_apply_three_way_success(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "print('a')\nline2 LOCAL\nline3\nline4\n")
    ov.register_overrides(repo, state_root)
    registry = ov.load_registry(state_root)
    # emulate a clean upstream checkout without the local change
    _git(repo, "checkout", "-q", "--", "hermes/a.py")
    result = ov.apply_overrides(repo, state_root, registry)
    assert result.ok, result.output
    assert "line2 LOCAL" in (repo / "hermes" / "a.py").read_text(encoding="utf-8")


@needs_git
def test_apply_reports_conflict_without_resolving(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "print('a')\nline2 LOCAL\nline3\nline4\n")
    ov.register_overrides(repo, state_root)
    registry = ov.load_registry(state_root)
    _modify(repo, "hermes/a.py", "print('a')\nline2 UPSTREAM\nline3\nline4\n")
    _git(repo, "add", "-A")
    base_result = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "upstream"],
        cwd=repo,
        capture_output=True,
    )
    assert base_result.returncode == 0
    result = ov.apply_overrides(repo, state_root, registry, dry_run=False)
    assert result.ok is False
    assert result.failed
    assert (repo / "hermes" / "a.py").read_text(encoding="utf-8").find("UPSTREAM") >= 0


@needs_git
def test_restore_clean_state_refuses_unknown(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "managed\n")
    _modify(repo, "hermes/b.py", "unknown\n")
    ov.register_overrides(repo, state_root, paths=["hermes/a.py"])
    report = ov.classify(repo, ov.load_registry(state_root))
    assert ov.restore_clean_state(repo, report) is False
    # nothing was touched
    assert "managed" in (repo / "hermes" / "a.py").read_text(encoding="utf-8")
    assert "unknown" in (repo / "hermes" / "b.py").read_text(encoding="utf-8")


@needs_git
def test_restore_clean_state_only_touches_managed_paths(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "managed\n")
    ov.register_overrides(repo, state_root, paths=["hermes/a.py"])
    report = ov.classify(repo, ov.load_registry(state_root))
    assert ov.restore_clean_state(repo, report) is True
    assert (repo / "hermes" / "a.py").read_text(encoding="utf-8").startswith("print('a')")


# --------------------------------------------------------------------------- #
# refresh / unregister / export / integrity / doctor
# --------------------------------------------------------------------------- #


@needs_git
def test_refresh_moves_the_baseline(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "v1\n")
    ov.register_overrides(repo, state_root)
    _modify(repo, "hermes/a.py", "v2\n")
    assert ov.classify(repo, ov.load_registry(state_root)).drifted == ["hermes/a.py"]
    result = ov.refresh_overrides(repo, state_root)
    assert result.refreshed == ["hermes/a.py"]
    report = ov.classify(repo, ov.load_registry(state_root))
    assert report.safety == ov.SAFETY_PASS
    assert report.drifted_count == 0


@needs_git
def test_unregister_is_forget_only(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "managed\n")
    ov.register_overrides(repo, state_root)
    removed = ov.unregister_overrides(state_root, ["hermes/a.py"])
    assert removed == ["hermes/a.py"]
    assert ov.load_registry(state_root).empty is True
    # the working tree still holds the user's edit - we only forgot it
    assert "managed" in (repo / "hermes" / "a.py").read_text(encoding="utf-8")


@needs_git
def test_export_bundle_contains_registry_and_patch(repo: Path, state_root: Path, tmp_path: Path) -> None:
    _modify(repo, "hermes/a.py", "managed\n")
    ov.register_overrides(repo, state_root)
    out = ov.export_overrides(state_root, tmp_path / "out" / "overrides.zip")
    assert out.is_file()
    with zipfile.ZipFile(out) as bundle:
        names = bundle.namelist()
    assert "overrides/registry.json" in names
    assert any(name.endswith(".patch") for name in names)
    assert "overrides/export.json" in names


@needs_git
def test_integrity_issues_clean_and_broken(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "managed\n")
    ov.register_overrides(repo, state_root)
    issues = {issue.key: issue.severity for issue in ov.integrity_issues(state_root, repo)}
    assert issues["override_registry"] == "ok"
    assert issues["override_patches"] == "ok"
    assert issues["override_patch_hash"] == "ok"
    assert issues["override_status"] == "ok"
    # corrupt a patch on purpose
    entry = ov.load_registry(state_root).files[0]
    Path(entry.patch_file).write_text("tampered\n", encoding="utf-8")
    issues = {issue.key: issue.severity for issue in ov.integrity_issues(state_root, repo)}
    assert issues["override_patch_hash"] == "fail"


@needs_git
def test_integrity_reports_fail_for_unknown_changes(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "managed\n")
    ov.register_overrides(repo, state_root)
    _modify(repo, "hermes/b.py", "unknown\n")
    issues = {issue.key: issue.severity for issue in ov.integrity_issues(state_root, repo)}
    assert issues["override_status"] == "fail"


def test_integrity_without_registry_is_ok(tmp_path: Path) -> None:
    issues = ov.integrity_issues(tmp_path / "state")
    assert [issue.severity for issue in issues] == ["ok"]


@needs_git
def test_integrity_flags_missing_patch_file(repo: Path, state_root: Path) -> None:
    _modify(repo, "hermes/a.py", "managed\n")
    ov.register_overrides(repo, state_root)
    entry = ov.load_registry(state_root).files[0]
    Path(entry.patch_file).unlink()
    Path(entry.snapshot_file).unlink()
    issues = {issue.key: issue.severity for issue in ov.integrity_issues(state_root, repo)}
    assert issues["override_patches"] == "fail"


# --------------------------------------------------------------------------- #
# no-git and stubbed-git environments
# --------------------------------------------------------------------------- #


def test_detect_without_git_returns_empty(tmp_path: Path) -> None:
    def dead_runner(cmd, **kwargs):
        return ProcResult(cmd=list(cmd), returncode=127, stdout="", stderr="git: not found")

    assert ov.detect_changes(tmp_path, runner=dead_runner) == []
    assert ov.git_available(install_dir=tmp_path, runner=dead_runner) is False


def test_classify_without_git_is_unknown(tmp_path: Path, state_root: Path) -> None:
    def dead_runner(cmd, **kwargs):
        return ProcResult(cmd=list(cmd), returncode=127, stdout="", stderr="git: not found")

    registry = ov.OverrideRegistry(files=[ov.OverrideEntry(path="a.py")])
    report = ov.classify(tmp_path, registry, runner=dead_runner)
    assert report.safety == ov.SAFETY_UNKNOWN


def test_classify_with_broken_registry_is_unknown(tmp_path: Path) -> None:
    registry = ov.OverrideRegistry(broken=True, broken_reason="nope")
    report = ov.classify(tmp_path, registry)
    assert report.safety == ov.SAFETY_UNKNOWN
    assert report.reasons_zh


def test_register_without_git_raises(tmp_path: Path, state_root: Path) -> None:
    def dead_runner(cmd, **kwargs):
        return ProcResult(cmd=list(cmd), returncode=127, stdout="", stderr="nope")

    with pytest.raises(ov.OverrideError):
        ov.register_overrides(tmp_path, state_root, runner=dead_runner)
