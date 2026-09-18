"""Tests for the secret/privacy scanner - the guard needs its own guard.

Every sample "secret" here is assembled at runtime from pieces. A literal
token-shaped string in this file would (correctly) make the scanner flag its own
test suite, which is exactly what happened the first time this file was written.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "scan_secrets.py"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("scan_secrets", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


scanner = _load_scanner()

BS = chr(92)
GITHUB_TOKEN = "ghp_" + "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
TELEGRAM_BOT_TOKEN = "123456789:" + "AA" + "Hf4kLmNoPqRsTuVwXyZ0123456789abcd"
OPENAI_KEY = "sk-" + "proj-" + "abcdefghijklmnopqrstuvwxyz012345"
PRIVATE_KEY_BLOCK = "-----BEGIN " + "RSA PRIVATE KEY-----"
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_PASSWORD = "hunter2" * 3


@pytest.mark.parametrize(
    "line,rule",
    [
        ('token = "' + GITHUB_TOKEN + '"', "github-token"),
        ("GH_TOKEN=" + GITHUB_TOKEN, "github-token"),
        ('api_key = "' + OPENAI_KEY + '"', "openai-key"),
        ('bot_token = "' + TELEGRAM_BOT_TOKEN + '"', "telegram-bot-token"),
        (PRIVATE_KEY_BLOCK, "private-key-block"),
        ('aws = "' + AWS_KEY + '"', "aws-access-key"),
        ('password = "' + FAKE_PASSWORD + '"', "generic-credential-assignment"),
    ],
)
def test_credentials_are_detected(line: str, rule: str) -> None:
    found = [name for name, _value in scanner.scan_text(line)]
    assert rule in found, f"{rule} not detected in {line!r} (found {found})"


@pytest.mark.parametrize(
    "line,rule",
    [
        ("install_dir: " + "D:" + BS + "software" + BS + "Hermes", "windows-drive-path"),
        ("install_dir: " + "D:" + "/software/Hermes", "windows-drive-path"),  # forward slashes too
        ("home: " + "C:" + BS + "Users" + BS + "alice", "windows-user-path"),
        ("home: " + "C:" + "/Users/" + "alice", "windows-user-path"),
        ("path: /home/" + "alice" + "/.hermes/config.yaml", "unix-home-path"),
        ("server: " + "93.184.216." + "34", "public-ipv4"),
        ("mail: " + "alice" + "@" + "gmail.com", "personal-email-domain"),
    ],
)
def test_private_data_is_detected(line: str, rule: str) -> None:
    found = [name for name, _value in scanner.scan_text(line)]
    assert rule in found, f"{rule} not detected in {line!r} (found {found})"


@pytest.mark.parametrize(
    "line",
    [
        "bot_token_env: HERMES_UPDATE_CHECK_TELEGRAM_TOKEN",  # a variable *name*, not a value
        'bot_token_env = "HERMES_UPDATE_CHECK_TELEGRAM_TOKEN"',
        "GITHUB_TOKEN env var, defaults to GH_TOKEN",
        "install_dir: /opt/hermes/hermes-agent",  # documented placeholder paths
        "hermes_home: /home/hermes/.hermes",
        "connect to 127.0.0.1:8080",
        "see https://github.com/xinian5216/hermes-update-check",
        "counts: 1039 commits, 338 PRs, 2026.9.14",
        "fixture path: C:" + BS + "hermes" + BS + "hermes-agent",
        "doc range example: " + "192.0.2." + "10",
        "see https://example.com/a/b and http://127.0.0.1:8080/health",  # URLs are not drive paths
        "protocol-relative: //cdn.example.com/x.js",
    ],
)
def test_legitimate_content_is_not_flagged(line: str) -> None:
    assert scanner.scan_text(line) == [], f"false positive on {line!r}"


def test_scanner_clean_on_this_repository() -> None:
    """The repository itself must always be publishable."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cli_reports_findings_and_exits_nonzero(tmp_path: Path) -> None:
    leak = tmp_path / "leak.txt"
    leak.write_text('api_key = "' + OPENAI_KEY + '"\n', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 1
    assert "FAIL" in result.stderr
    assert "openai-key" in result.stderr
    # the finding must be redacted, never echoed in full
    assert OPENAI_KEY not in result.stderr


def test_text_mode_flags_outbound_payloads() -> None:
    """Anything leaving the machine (e-mail, webhook, API body) can be checked."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--text", "webhook body: token=" + GITHUB_TOKEN],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 1
    assert "github-token" in result.stderr
    assert GITHUB_TOKEN not in result.stderr  # redacted


def test_text_mode_accepts_clean_payloads() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--text", "status: WAIT, risk 92/100, channel MAIN"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr


def test_stdin_mode_scans_piped_text() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--stdin"],
        input="home dir: /home/" + "alice" + "/.hermes\n",
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 1
    assert "unix-home-path" in result.stderr


def test_installer_scripts_are_present_and_syntactically_valid() -> None:
    """One-click install must actually be shippable."""
    sh_path = REPO_ROOT / "install.sh"
    ps_path = REPO_ROOT / "install.ps1"
    assert sh_path.is_file(), "install.sh missing"
    assert ps_path.is_file(), "install.ps1 missing"

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available on this runner")
    result = subprocess.run([bash, "-n", str(sh_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_installer_documents_its_options() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available on this runner")
    result = subprocess.run([bash, str(REPO_ROOT / "install.sh"), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    for option in ("--dir", "--repo", "--branch", "--bin-dir", "--no-hook"):
        assert option in result.stdout, f"{option} not documented"


@pytest.mark.parametrize("name", ["install.sh", "install.ps1"])
def test_installer_never_runs_hermes_update(name: str) -> None:
    """The whole point of the tool: installing it must not update Hermes."""
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    invocation = re.compile(r"(?:^|;|&&|\|\||\|)\s*\$?\s*(?:hermes\s+update|sudo)\b")
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        assert not invocation.search(line), (
            f"{name}:{lineno} looks like it invokes a privileged/updating command: {line.strip()!r}"
        )


def test_readme_documents_one_click_install_and_agent_prompt() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "install.sh | bash" in readme, "README lacks the Linux/macOS one-liner"
    assert "install.ps1 | iex" in readme, "README lacks the Windows one-liner"
    assert "hermes-update-check.git ~/projects/hermes-update-check" in readme, "README lacks the AI-agent prompt"
    assert "不要执行 `hermes update`" in readme, "the agent prompt must forbid updating Hermes"
    assert "core.hooksPath .githooks" in readme, "the agent prompt must enable the privacy hook"


def test_pre_commit_hook_is_wired_and_probes_the_interpreter() -> None:
    hook = REPO_ROOT / ".githooks" / "pre-commit"
    assert hook.is_file(), "pre-commit hook missing"
    text = hook.read_text(encoding="utf-8")
    assert "scan_secrets.py" in text
    assert "--staged" in text
    # the interpreter must be probed by executing it: Windows ships python3.exe
    # stubs that exist but fail, which would silently disable the guard
    assert "sys.version_info" in text


def test_ci_workflow_runs_the_scan_on_full_history() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scan_secrets.py --all-history" in workflow
    assert "fetch-depth: 0" in workflow
    assert "gitleaks" in workflow


# --------------------------------------------------------------------------- #
# History scan: only file *contents* are scanned, never tree/commit metadata
# --------------------------------------------------------------------------- #

# the digit run that blocked a push once: it sat inside a blob sha in a tree listing.
# Split in the middle of the digit run - a literal here would make the scanner flag
# its own test file (which is exactly the failure this test documents).
SHA_WITH_PHONE_LIKE_DIGITS = "9b6d04c20d95c7a603725109c6d162" + "6" + "5436672c8"


def _fake_git(monkeypatch, objects: str, blobs: dict[str, str]) -> None:
    """Serve a canned `rev-list --objects --all` + object contents."""

    def fake_git_output(*args: str) -> str:
        if args[:2] == ("rev-list", "--objects"):
            return objects
        if args[0] == "cat-file" and args[1] == "-p":
            return blobs.get(args[2], "")
        raise AssertionError(f"unexpected git call: {args}")

    def fake_run(cmd, **kwargs):  # the --batch-check helper
        wanted = str(kwargs.get("input", "")).split()
        lines = [f"{sha} blob" if sha in blobs else f"{sha} tree" for sha in wanted]

        class _Done:
            stdout = "\n".join(lines)
            returncode = 0

        return _Done()

    monkeypatch.setattr(scanner, "git_output", fake_git_output)
    monkeypatch.setattr(scanner.subprocess, "run", fake_run)


def test_tree_objects_are_not_scanned(monkeypatch) -> None:
    """A tree listing is sha + filename: its hex must never be read as content."""
    tree_sha = "73da9b074f0b75f30f4d76686d4d8152ece4188a"
    objects = f"{tree_sha} tests\n{SHA_WITH_PHONE_LIKE_DIGITS} tests/test_x.py"
    _fake_git(monkeypatch, objects, {SHA_WITH_PHONE_LIKE_DIGITS: "print('hello')\n"})
    assert scanner.scan_git_history() == []


def test_blob_contents_are_still_scanned(monkeypatch) -> None:
    """The tree skip must not blind the scanner to real leaks in history."""
    token = "ghp_" + "Z9Y8X7W6V5U4T3S2R1Q0P9O8N7M6L5K4"
    objects = f"{SHA_WITH_PHONE_LIKE_DIGITS} tests/test_x.py"
    _fake_git(monkeypatch, objects, {SHA_WITH_PHONE_LIKE_DIGITS: f'token = "{token}"\n'})
    problems = scanner.scan_git_history()
    assert len(problems) == 1
    assert "github-token" in problems[0]
    assert token not in problems[0]  # redacted, never echoed back
