"""Version parsing and comparison.

Hermes uses two version schemes in parallel, and the tool must understand both:

* a SemVer-ish display version - ``0.21.3`` (from ``hermes --version`` or
  ``hermes_cli.__version__``);
* a date-based release tag - ``v2026.9.14`` (``git tag`` / GitHub release tag).

``hermes --version`` prints both on the first line::

    Hermes Agent v0.21.2 (2026.9.11) · upstream 5eb99eb2

so ``parse_version_output`` understands that line and tolerates older layouts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #

_SEMVER_RE = re.compile(
    r"""
    ^\s*v?
    (?P<major>\d+)
    \.(?P<minor>\d+)
    (?:\.(?P<patch>\d+))?
    (?:[-_.]?(?P<pre>alpha|beta|rc|pre|dev)\.?(?P<pre_n>\d+)?)?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_CALVER_RE = re.compile(r"^v?(?P<year>(?:19|20)\d{2})\.(?P<month>\d{1,2})\.(?P<day>\d{1,2})(?:\.(?P<seq>\d+))?$")

_ANY_VERSION_RE = re.compile(r"\bv?(\d+\.\d+(?:\.\d+)?(?:[-_.]?(?:rc|beta|alpha|pre|dev)\.?\d*)?)", re.IGNORECASE)

_PRE_RANK = {"dev": 0, "alpha": 1, "beta": 2, "pre": 2, "rc": 3}


@dataclass(frozen=True)
class Version:
    """A comparable semantic version."""

    major: int
    minor: int
    patch: int = 0
    prerelease: Tuple[str, int] = ("", 0)
    raw: str = ""

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease[0])

    @property
    def key(self) -> Tuple[int, int, int, int, int]:
        rank = _PRE_RANK.get(self.prerelease[0], 9) if self.prerelease[0] else 99
        return (self.major, self.minor, self.patch, rank, self.prerelease[1])

    def __str__(self) -> str:
        return self.raw or f"{self.major}.{self.minor}.{self.patch}"


def parse_version(text: str | None) -> Optional[Version]:
    """Parse ``v0.21.3`` / ``0.21`` / ``0.21.3-rc1`` into a Version (None when hopeless)."""
    if not text:
        return None
    match = _SEMVER_RE.match(str(text).strip())
    if match:
        major = int(match.group("major"))
        minor = int(match.group("minor"))
        patch = int(match.group("patch") or 0)
        pre_name = (match.group("pre") or "").lower()
        pre_num = int(match.group("pre_n") or 0)
        return Version(major, minor, patch, (pre_name, pre_num), str(text).strip())

    # last resort: grab the first version-looking token inside the string
    found = _ANY_VERSION_RE.search(str(text))
    if found and found.group(1) != str(text).strip():
        return parse_version(found.group(1))
    return None


def semver_key(text: str | None) -> Tuple[int, int, int, int, int]:
    """Sort key for a version string; unparseable versions sort lowest."""
    version = parse_version(text)
    return version.key if version else (-1, -1, -1, -1, -1)


def compare_versions(left: str | None, right: str | None) -> int:
    """-1 if left < right, 0 if equal, 1 if left > right (unparseable sorts last)."""
    lk, rk = semver_key(left), semver_key(right)
    if lk == rk:
        return 0
    return -1 if lk < rk else 1


def parse_calver(text: str | None) -> Optional[Tuple[int, int, int, int]]:
    """Parse date tags such as ``v2026.9.14`` / ``2026.8.16.2``."""
    if not text:
        return None
    match = _CALVER_RE.match(str(text).strip())
    if not match:
        return None
    return (
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        int(match.group("seq") or 0),
    )


def compare_tags(left: str | None, right: str | None) -> int:
    """Compare two release tags: prefer calver, fall back to semver, then string order."""
    lc, rc = parse_calver(left), parse_calver(right)
    if lc and rc:
        if lc == rc:
            return 0
        return -1 if lc < rc else 1
    if left and right:
        lv, rv = parse_version(left), parse_version(right)
        if lv and rv:
            return compare_versions(lv.raw, rv.raw)
        return -1 if str(left) < str(right) else (0 if left == right else 1)
    if left is None and right is None:
        return 0
    return -1 if left is None else 1


def classify_bump(current: str | None, target: str | None) -> str:
    """'major' | 'minor' | 'patch' | 'none' | 'unknown'."""
    cur, tgt = parse_version(current), parse_version(target)
    if cur is None or tgt is None:
        return "unknown"
    if tgt.key == cur.key:
        return "none"
    if tgt.major != cur.major:
        return "major"
    if tgt.minor != cur.minor:
        return "minor"
    return "patch"


def is_feature_release(version_text: str | None) -> bool:
    """X.Y.0 releases carry new features and deserve extra caution."""
    version = parse_version(version_text)
    return bool(version and version.patch == 0)


def extract_versions(text: str | None) -> list[str]:
    """Every version-looking token in a blob of text (release notes, changelogs)."""
    if not text:
        return []
    return [m.group(0) for m in _ANY_VERSION_RE.finditer(text)]


def is_prerelease_text(text: str | None) -> bool:
    """True for prerelease/beta/rc markers inside a version or a release title."""
    if not text:
        return False
    lowered = str(text).lower()
    if re.search(r"\b(alpha|beta|rc|pre[-\s]?release|canary|nightly)\d{0,3}\b", lowered):
        return True
    version = parse_version(text)
    return bool(version and version.is_prerelease)


# --------------------------------------------------------------------------- #
# `hermes --version` output
# --------------------------------------------------------------------------- #

_HEADLINE_RE = re.compile(
    r"""
    Hermes\s+Agent
    (?:\s+version)?          # older builds printed "Hermes Agent version 0.20.1"
    \s+v?(?P<version>\d+\.\d+(?:\.\d+)?(?:[-_.]?(?:rc|beta|alpha|dev)\.?\d*)?)
    (?:\s*\(\s*(?P<tag>v?\d{4}\.\d{1,2}\.\d{1,2}(?:\.\d+)?)\s*\))?
    (?:.*?upstream\s+(?P<sha>[0-9a-fA-F]{6,40}))?
    """,
    re.IGNORECASE | re.VERBOSE,
)
_INSTALL_METHOD_RE = re.compile(r"Install method:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_INSTALL_DIR_RE = re.compile(r"Install director(?:y|ies):\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_PYTHON_RE = re.compile(r"^Python:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_SDK_RE = re.compile(r"^(?:OpenAI\s+SDK|SDK):\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_UPTODATE_RE = re.compile(
    r"\b(up to date|already up[- ]to[- ]date|no update|update available|new version)\b", re.IGNORECASE
)


@dataclass
class VersionOutput:
    """Structured form of `hermes --version`."""

    raw: str = ""
    version: Optional[str] = None
    release_tag: Optional[str] = None
    commit: Optional[str] = None
    install_dir: Optional[str] = None
    install_method: Optional[str] = None
    python_version: Optional[str] = None
    sdk_version: Optional[str] = None
    says_up_to_date: Optional[bool] = None
    parsed: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def version_or_unknown(self) -> str:
        return self.version or "unknown"


def parse_version_output(text: str | None) -> VersionOutput:
    """Parse the output of ``hermes --version`` across known layouts."""
    out = VersionOutput(raw=text or "")
    if not text:
        out.notes.append("empty version output")
        return out

    headline = _HEADLINE_RE.search(text)
    if headline:
        out.version = headline.group("version")
        out.release_tag = _normalise_tag(headline.group("tag"))
        out.commit = headline.group("sha")
        out.parsed = True
    else:
        candidates = extract_versions(text)
        if candidates:
            out.version = candidates[0].lstrip("v")
            out.parsed = True
            out.notes.append("headline unrecognised; used first version token")
        else:
            out.notes.append("no version token found in output")

    method = _INSTALL_METHOD_RE.search(text)
    if method:
        out.install_method = method.group(1).strip().lower()
    directory = _INSTALL_DIR_RE.search(text)
    if directory:
        out.install_dir = directory.group(1).strip()
    python_version = _PYTHON_RE.search(text)
    if python_version:
        out.python_version = python_version.group(1).strip()
    sdk = _SDK_RE.search(text)
    if sdk:
        out.sdk_version = sdk.group(1).strip()

    flat = text.lower()
    if "up to date" in flat:
        out.says_up_to_date = True
    elif "update available" in flat or "new version" in flat:
        out.says_up_to_date = False
    return out


def _normalise_tag(tag: str | None) -> Optional[str]:
    if not tag:
        return None
    tag = tag.strip()
    if not tag.startswith("v") and parse_calver(tag):
        tag = "v" + tag
    return tag


def normalise_tag(tag: str | None) -> Optional[str]:
    """Public alias: ``2026.9.14`` -> ``v2026.9.14``."""
    return _normalise_tag(tag)


def latest_by_version(items: Iterable[str]) -> Optional[str]:
    """Highest version in a list of version strings (None for an empty list)."""
    best: Optional[str] = None
    for item in items:
        if best is None or compare_versions(item, best) > 0:
            best = item
    return best


def versions_between(current: str | None, ordered_desc: Sequence[str]) -> Optional[int]:
    """How many releases sit between ``current`` and the newest entry of a descending list.

    Returns ``0`` when ``current`` is already the newest known release and
    ``None`` when ``current`` is not present in the list at all.
    """
    if not ordered_desc:
        return None
    keys = [semver_key(x) for x in ordered_desc]
    if current is None:
        return None
    current_key = semver_key(current)
    if current_key[0] < 0:
        return None
    if current_key not in keys:
        # count releases strictly newer than current
        return sum(1 for k in keys if k > current_key) or None
    return keys.index(current_key)
