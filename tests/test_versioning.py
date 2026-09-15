"""Version parsing/comparison tests - the part that must never be wrong."""

from __future__ import annotations

import pytest

from hermes_update_check.versioning import (
    classify_bump,
    compare_tags,
    compare_versions,
    extract_versions,
    is_feature_release,
    is_prerelease_text,
    latest_by_version,
    normalise_tag,
    parse_calver,
    parse_version,
    parse_version_output,
    versions_between,
)

REAL_VERSION_OUTPUT = """Hermes Agent v0.21.2 (2026.9.11) - upstream 5eb99eb2
Install directory: D:\\software\\Hermes\\hermes-agent
Install method: git
Python: 3.11.16
OpenAI SDK: 2.24.0
Up to date
"""


def test_parse_version_output_real_layout() -> None:
    parsed = parse_version_output(REAL_VERSION_OUTPUT)
    assert parsed.parsed is True
    assert parsed.version == "0.21.2"
    assert parsed.release_tag == "v2026.9.11"
    assert parsed.commit == "5eb99eb2"
    assert parsed.install_dir == "D:\\software\\Hermes\\hermes-agent"
    assert parsed.install_method == "git"
    assert parsed.python_version == "3.11.16"
    assert parsed.sdk_version == "2.24.0"
    assert parsed.says_up_to_date is True


def test_parse_version_output_legacy_layout() -> None:
    parsed = parse_version_output("Hermes Agent version 0.20.1\nUpdate available\n")
    assert parsed.version == "0.20.1"
    assert parsed.release_tag is None
    assert parsed.says_up_to_date is False


def test_parse_version_output_unparseable() -> None:
    parsed = parse_version_output("command not found: hermes")
    assert parsed.parsed is False
    assert parsed.version is None
    assert parsed.notes


def test_parse_version_variants() -> None:
    assert parse_version("v0.21.3").raw == "v0.21.3"
    assert parse_version("0.21").patch == 0
    rc = parse_version("0.22.0-rc1")
    assert rc is not None and rc.is_prerelease
    assert parse_version("not a version") is None


def test_compare_versions_and_prereleases() -> None:
    assert compare_versions("0.21.3", "0.21.2") == 1
    assert compare_versions("0.21.2", "0.21.2") == 0
    assert compare_versions("0.20.9", "0.21.0") == -1
    # a prerelease ranks below its own release
    assert compare_versions("0.22.0-rc1", "0.22.0") == -1


def test_calver_tags() -> None:
    assert parse_calver("v2026.9.14") == (2026, 9, 14, 0)
    assert parse_calver("2026.8.16.2") == (2026, 8, 16, 2)
    assert parse_calver("0.21.3") is None
    assert compare_tags("v2026.9.14", "v2026.9.11") == 1
    assert compare_tags("v2026.8.16.2", "v2026.8.16") == 1
    assert normalise_tag("2026.9.14") == "v2026.9.14"


def test_classify_bump() -> None:
    assert classify_bump("0.21.2", "0.21.3") == "patch"
    assert classify_bump("0.21.2", "0.22.0") == "minor"
    assert classify_bump("0.21.2", "1.0.0") == "major"
    assert classify_bump("0.21.2", "0.21.2") == "none"
    assert classify_bump(None, "0.21.2") == "unknown"


def test_feature_release_detection() -> None:
    assert is_feature_release("0.21.0") is True
    assert is_feature_release("0.21.3") is False
    assert is_feature_release(None) is False


def test_prerelease_text() -> None:
    assert is_prerelease_text("0.22.0-beta1") is True
    assert is_prerelease_text("Hermes Agent v0.22.0 rc2") is True
    assert is_prerelease_text("0.21.3") is False


def test_extract_and_latest() -> None:
    text = "Fixes for 0.21.1, 0.21.2 and v0.21.3 are included"
    assert "0.21.1" in extract_versions(text)
    assert latest_by_version(["0.21.1", "0.21.3", "0.21.2"]) == "0.21.3"


def test_versions_between() -> None:
    releases = ["0.21.3", "0.21.2", "0.21.1", "0.21.0"]
    assert versions_between("0.21.3", releases) == 0
    assert versions_between("0.21.2", releases) == 1
    assert versions_between("0.21.0", releases) == 3
    # not in the list -> count of strictly newer entries
    assert versions_between("0.20.9", releases) == 4
    assert versions_between(None, releases) is None
    assert versions_between("0.21.2", []) is None


@pytest.mark.parametrize(
    "tag,expected",
    [("v2026.9.14", (2026, 9, 14, 0)), ("v2026.8.16.2", (2026, 8, 16, 2))],
)
def test_calver_parametrised(tag: str, expected: tuple[int, int, int, int]) -> None:
    assert parse_calver(tag) == expected
