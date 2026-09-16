"""Environment-detection tests: the layer that reads the *real* machine.

Everything here runs against a fake machine - a temporary HERMES_HOME plus a
stubbed ``run_process`` - so no test depends on what is installed locally.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_update_check.config import Config
from hermes_update_check.local_env import (
    ProcessInfo,
    _find_venv_python,
    _infer_install_kind,
    _list_windows_processes_via_tasklist,
    _normalise_install_kind,
    _version_from_source,
    detect_git_state,
    detect_local_env,
    gateway_status,
    list_hermes_processes,
    parse_gateway_status,
    require_hermes_home,
)
from hermes_update_check.util import ProcResult

VERSION_OUTPUT = """Hermes Agent v0.21.2 (2026.9.11) - upstream 5eb99eb2
Install directory: /opt/hermes/hermes-agent
Install method: git
Python: 3.11.16
OpenAI SDK: 2.24.0
Up to date
"""


def stub_process(
    monkeypatch: pytest.MonkeyPatch, output: str, *, returncode: int = 0, error: str | None = None
) -> None:
    def fake_run(cmd, **kwargs):
        return ProcResult(cmd=[str(c) for c in cmd], returncode=returncode, stdout=output, error=error)

    monkeypatch.setattr("hermes_update_check.local_env.run_process", fake_run)


# --------------------------------------------------------------------------- #
# install kind
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("git", "git"),
        ("Install method: git", "git"),
        ("installed with pipx", "pip"),
        ("uv-managed", "uv"),
        ("docker container", "docker"),
        ("nix profile", "nix"),
        ("something else entirely", "unknown"),
        ("", "unknown"),
    ],
)
def test_normalise_install_kind(raw: str, expected: str) -> None:
    assert _normalise_install_kind(raw) == expected


def test_infer_install_kind_prefers_git_checkout(tmp_path: Path) -> None:
    (tmp_path / "hermes-agent" / ".git").mkdir(parents=True)
    assert _infer_install_kind(tmp_path, log=__import__("logging").getLogger("t")) == "git"


def test_infer_install_kind_detects_nix(tmp_path: Path) -> None:
    (tmp_path / "flake.nix").write_text("{ }", encoding="utf-8")
    assert _infer_install_kind(tmp_path, log=__import__("logging").getLogger("t")) == "nix"


def test_infer_install_kind_probes_a_venv_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv_python = tmp_path / "hermes-agent" / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    stub_process(monkeypatch, "Name: hermes-agent\nVersion: 0.21.2\n")
    assert _infer_install_kind(tmp_path, log=__import__("logging").getLogger("t")) == "pip"


def test_infer_install_kind_gives_up_cleanly(tmp_path: Path) -> None:
    assert _infer_install_kind(tmp_path, log=__import__("logging").getLogger("t")) == "unknown"


def test_version_from_source_reads_the_package_file(tmp_path: Path) -> None:
    package = tmp_path / "hermes_cli"
    package.mkdir()
    (package / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    assert _version_from_source(tmp_path) == "9.9.9"
    assert _version_from_source(tmp_path / "missing") is None


def test_find_venv_python_layouts(tmp_path: Path) -> None:
    win = tmp_path / "hermes-agent" / "venv" / "Scripts" / "python.exe"
    win.parent.mkdir(parents=True)
    win.write_text("", encoding="utf-8")
    assert _find_venv_python(tmp_path, None) == win

    posix_home = tmp_path / "home"
    posix = posix_home / "venv" / "bin" / "python"
    posix.parent.mkdir(parents=True)
    posix.write_text("", encoding="utf-8")
    assert _find_venv_python(posix_home, None) == posix
    assert _find_venv_python(tmp_path / "empty", None) is None


# --------------------------------------------------------------------------- #
# version output / full detection
# --------------------------------------------------------------------------- #
def test_detect_local_env_parses_a_fake_installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "hermes-home"
    (home / "hermes-agent" / "hermes_cli").mkdir(parents=True)
    (home / "config.yaml").write_text("model: k3\n", encoding="utf-8")
    stub_process(monkeypatch, VERSION_OUTPUT)

    cfg = Config()
    cfg.paths.hermes_home = home
    env = detect_local_env(cfg, run_upstream_check=False)

    assert env.version == "0.21.2"
    assert env.release_tag == "v2026.9.11"
    assert env.install_kind == "git"
    assert env.hermes_home == home
    assert env.python_version == "3.11.16"
    assert env.says_up_to_date is True
    assert env.to_dict()["version"] == "0.21.2"


def test_detect_local_env_without_a_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_process(monkeypatch, "", returncode=127, error="not found")
    cfg = Config()
    cfg.paths.hermes_home = tmp_path / "empty-home"

    env = detect_local_env(cfg, run_upstream_check=False)

    assert env.version is None
    assert env.install_kind in {"unknown", "pip", "uv"}  # whatever the fallback concludes
    assert env.to_dict() is not None  # must still be serialisable


# --------------------------------------------------------------------------- #
# git state
# --------------------------------------------------------------------------- #
def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    )
    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "test")
    (path / "file.txt").write_text("hi\n", encoding="utf-8")
    run("add", "file.txt")
    run("commit", "-q", "-m", "initial")
    return path


def test_detect_git_state_on_a_real_repository(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    state = detect_git_state(repo)

    assert state.is_repo is True
    assert state.branch == "main"
    assert state.on_main is True
    assert state.dirty is False
    assert state.commit and len(state.commit) >= 7
    assert state.full_commit and len(state.full_commit) == 40
    assert state.error is None


def test_detect_git_state_sees_uncommitted_changes(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo2")
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")
    state = detect_git_state(repo)
    assert state.dirty is True
    assert state.dirty_files and "file.txt" in state.dirty_files[0]


def test_detect_git_state_outside_a_repository(tmp_path: Path) -> None:
    state = detect_git_state(tmp_path / "not-a-repo")
    assert state.is_repo is False


# --------------------------------------------------------------------------- #
# processes / gateway
# --------------------------------------------------------------------------- #
def test_tasklist_parsing_keeps_only_hermes_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    csv = (
        '"chrome.exe","1234","Console","1","100 K"\n'
        '"Hermes.exe","2222","Console","1","500 K"\n'
        '"python.exe","3333","Console","1","300 K"\n'
        '"hermes-gateway.exe","4444","Console","1","200 K"\n'
    )
    stub_process(monkeypatch, csv)
    processes, error = _list_windows_processes_via_tasklist(timeout=5)

    assert error is None
    assert [p.pid for p in processes] == [2222, 4444]


def test_tasklist_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_process(monkeypatch, "", returncode=1, error="tasklist failed")
    processes, error = _list_windows_processes_via_tasklist(timeout=5)
    assert processes == [] and error == "tasklist failed"


def test_list_hermes_processes_dispatches_by_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_update_check.local_env._list_posix_processes",
        lambda *, timeout: ([ProcessInfo(pid=1, args="hermes serve")], None),
    )
    monkeypatch.setattr(
        "hermes_update_check.local_env._list_windows_processes",
        lambda *, timeout: ([ProcessInfo(pid=2, args="Hermes.exe")], None),
    )

    processes, error = list_hermes_processes(timeout=5)

    assert error is None
    assert processes[0].pid == (2 if os.name == "nt" else 1)


def test_gateway_status_without_a_cli() -> None:
    result = gateway_status(None)
    assert result.ok is False
    status, detail = parse_gateway_status(result)
    assert status == "WARN" and "query gateway" in detail


@pytest.mark.parametrize(
    "output,returncode,expected_status",
    [
        ("gateway is not running", 1, "WARN"),
        ("service stopped", 3, "WARN"),
        ("inactive (dead)", 3, "WARN"),
        ("gateway is running (pid 42)", 0, "PASS"),
        ("active (running)", 0, "PASS"),
        ("healthy", 0, "PASS"),
        ("something else", 0, "PASS"),
        ("something else", 9, "WARN"),
    ],
)
def test_parse_gateway_status_classification(output: str, returncode: int, expected_status: str) -> None:
    result = ProcResult(cmd=["hermes", "gateway", "status"], returncode=returncode, stdout=output)
    status, _detail = parse_gateway_status(result)
    assert status == expected_status


def test_parse_gateway_status_empty_output() -> None:
    status, detail = parse_gateway_status(ProcResult(cmd=["x"], returncode=0, stdout="  "))
    assert status == "WARN" and "no output" in detail


def test_require_hermes_home_raises_when_missing(tmp_path: Path) -> None:
    from hermes_update_check.errors import ConfigError

    cfg = Config()
    cfg.paths.hermes_home = tmp_path / "gone"
    with pytest.raises(ConfigError):
        require_hermes_home(cfg)

    (tmp_path / "present").mkdir()
    cfg.paths.hermes_home = tmp_path / "present"
    assert require_hermes_home(cfg) == tmp_path / "present"
