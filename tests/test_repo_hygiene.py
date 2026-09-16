"""Repository hygiene: nothing important may be silently ignored or stale.

Regression guard for a real incident: the `.gitignore` pattern `*_secret*` (meant to
block secret *files*) also matched `tests/test_scan_secrets.py`, so the tests for the
privacy scanner were never committed - they ran locally and were absent in CI, which
only surfaced as a "code map is stale" failure.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GIT = shutil.which("git")


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(GIT), *args], cwd=ROOT, capture_output=True, text=True)


def tracked_files() -> set[str]:
    result = git("ls-files")
    assert result.returncode == 0, result.stderr
    return {line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()}


pytestmark = pytest.mark.skipif(GIT is None, reason="git is not available")


def test_every_source_file_is_tracked() -> None:
    """A file that exists but is ignored would never reach CI."""
    tracked = tracked_files()
    candidates: list[Path] = []
    for pattern, folder in (("*.py", "src"), ("*.py", "tests"), ("*.py", "scripts")):
        candidates.extend(p for p in (ROOT / folder).rglob(pattern) if "__pycache__" not in p.parts)
    candidates.extend(
        ROOT / name for name in ("install.sh", "install.ps1", "AGENTS.md", "README.md", "SECURITY.md", "CHANGELOG.md")
    )

    missing = sorted(
        str(path.relative_to(ROOT)).replace("\\", "/")
        for path in candidates
        if str(path.relative_to(ROOT)).replace("\\", "/") not in tracked
    )

    assert not missing, "these files exist but are not tracked by git: " + ", ".join(missing)


def test_the_guard_files_themselves_are_tracked() -> None:
    tracked = tracked_files()
    for name in (
        ".githooks/pre-commit",
        ".github/workflows/ci.yml",
        "scripts/scan_secrets.py",
        "scripts/build_index.py",
        "docs/CODE_MAP.md",
        "docs/index.json",
        "config.example.yaml",
    ):
        assert name in tracked, f"{name} is not tracked - CI would not see it"


def test_code_map_is_up_to_date() -> None:
    """Same check CI runs: the map must match the source it describes."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_index.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
