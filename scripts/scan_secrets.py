#!/usr/bin/env python3
"""Fail the build if a secret or personal path is about to be published.

Two layers:

* **pattern rules** - credential formats that are never legitimate in a repo
  (GitHub tokens, OpenAI keys, Telegram bot tokens, PEM blocks, JWTs, cloud keys)
  plus generic ``password = "..."`` style assignments;
* **privacy rules** - absolute Windows paths, real home directories, public IP
  addresses, personal e-mail domains and this project's own machine markers.

Only stdlib is used, so it runs on a bare VPS as well as in the pre-commit hook
and in CI. Matches are printed redacted; exit code is 1 when anything is found.

Usage::

    python scripts/scan_secrets.py                 # scan tracked files
    python scripts/scan_secrets.py --staged        # scan the git index (pre-commit)
    python scripts/scan_secrets.py --all-history   # scan every blob ever committed
    python scripts/scan_secrets.py path/to/dir     # scan an arbitrary tree
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #

#: (rule name, regex). Order matters only for reporting.
SECRET_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("telegram-bot-token", re.compile(r"\b\d{7,12}:AA[A-Za-z0-9_-]{30,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")),
    ("pypi-token", re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{20,}\b")),
    ("stripe-secret", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    (
        "generic-credential-assignment",
        re.compile(
            r"""(?ix)
            \b(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret|secret[_-]?key|password|passwd|bot[_-]?token)\b
            \s*[:=]\s*
            ["'](?!\$|\{|\<|xxx|yyy|your|example|changeme|placeholder|redacted|\*)[^"'\s]{12,}["']
            """
        ),
    ),
]

#: (rule name, regex, what to say when it fires)
PRIVACY_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("windows-user-path", re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"'`]+"), "absolute Windows user path"),
    ("windows-drive-path", re.compile(r"\b[A-Za-z]:\\(?!Users\\)[A-Za-z0-9_.-]+"), "absolute Windows path"),
    (
        "unix-home-path",
        re.compile(r"/(?:home|Users)/(?!hermes\b|user\b|you\b|example\b)[a-z0-9._-]{3,}"),
        "real unix home directory",
    ),
    ("public-ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "public IP address"),
    (
        "personal-email-domain",
        re.compile(r"\b[A-Za-z0-9._%+-]+@(?:qq|163|126|gmail|outlook|hotmail|foxmail|sina|yeah|icloud)\.com\b"),
        "personal e-mail address",
    ),
    ("china-mobile-number", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "phone-number-like digit run"),
]

#: strings that are fine even though a privacy rule matches them
ALLOWLIST: list[str] = [
    "127.0.0.1",
    "0.0.0.0",
    "255.255.255.255",
    "1.2.3.4",
    "8.8.8.8",
    "1.1.1.1",
    "192.168.0.1",
    "10.0.0.1",
    "192.0.2.1",  # RFC 5737 documentation range
    "198.51.100.1",
    "203.0.113.1",
    "C:" + chr(92) + "hermes",  # placeholder install path used in test fixtures
    "C:" + chr(92) + "Users" + chr(92) + "...",  # the literal in the README's rule description
]

#: file types we do not scan (binary or generated)
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar", ".whl",
    ".exe", ".dll", ".so", ".dylib", ".pyc", ".woff", ".woff2", ".ttf", ".mp3", ".mp4",
}

#: paths we never scan (vendored deps, caches, VCS internals)
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", ".mypy_cache"}


def redact(text: str) -> str:
    """Show enough to locate the problem, not enough to leak it."""
    for _name, pattern in SECRET_RULES:
        text = pattern.sub(lambda m: m.group(0)[:6] + "…<redacted>", text)
    return text


def iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def scan_text(text: str) -> list[tuple[str, str]]:
    """Return (rule, matched-text) for one file's content."""
    found: list[tuple[str, str]] = []
    for name, pattern in SECRET_RULES:
        for match in pattern.finditer(text):
            value = match.group(0)
            if value in ALLOWLIST:
                continue
            found.append((name, value))
    for name, pattern, _why in PRIVACY_RULES:
        for match in pattern.finditer(text):
            value = match.group(0)
            if value in ALLOWLIST:
                continue
            found.append((name, value))
    return found


def scan_file(path: Path, *, root: Path | None = None) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:  # pragma: no cover - unreadable file
        return [f"{path}: unreadable ({exc})"]
    label = str(path.relative_to(root)) if root else str(path)
    problems: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for rule, value in scan_text(line):
            problems.append(f"{label}:{lineno}: [{rule}] {redact(value)[:80]}")
    return problems


def git_output(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, errors="ignore"
    ).stdout


def scan_git_history() -> list[str]:
    """Scan every blob reachable from any ref (catches removed-but-committed data)."""
    problems: list[str] = []
    seen: set[str] = set()
    for rev in git_output("rev-list", "--objects", "--all").splitlines():
        parts = rev.split(maxsplit=1)
        if len(parts) != 2:
            continue
        sha, name = parts
        if sha in seen or Path(name).suffix.lower() in SKIP_SUFFIXES:
            continue
        seen.add(sha)
        try:
            blob = git_output("cat-file", "-p", sha)
        except subprocess.CalledProcessError:
            continue
        for lineno, line in enumerate(blob.splitlines(), 1):
            for rule, value in scan_text(line):
                problems.append(f"{name}@{sha[:8]}:{lineno}: [{rule}] {redact(value)[:80]}")
    return problems


def scan_staged() -> list[str]:
    problems: list[str] = []
    for line in git_output("diff", "--cached", "--name-only", "--diff-filter=ACM").splitlines():
        path = Path(line)
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            blob = git_output("show", f":{line}")
        except subprocess.CalledProcessError:
            continue
        for lineno, content in enumerate(blob.splitlines(), 1):
            for rule, value in scan_text(content):
                problems.append(f"{line}:{lineno}: [{rule}] {redact(value)[:80]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan for secrets and privacy leaks before publishing.")
    parser.add_argument("paths", nargs="*", help="files or directories (default: current directory)")
    parser.add_argument("--staged", action="store_true", help="scan only what is staged for commit")
    parser.add_argument("--all-history", action="store_true", help="scan every blob in git history")
    args = parser.parse_args(argv)

    problems: list[str] = []
    if args.staged:
        problems += scan_staged()
    if args.all_history:
        problems += scan_git_history()
    if not args.staged and not args.all_history:
        roots = [Path(p) for p in args.paths] or [Path.cwd()]
        for root in roots:
            if root.is_file():
                problems += scan_file(root)
            else:
                for path in iter_files(root):
                    problems += scan_file(path, root=root)

    if problems:
        print(f"FAIL: {len(problems)} potential secret/privacy leak(s):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("\nMove the value into an environment variable (see README '安全边界').", file=sys.stderr)
        return 1

    print("OK: no secrets or private paths found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
