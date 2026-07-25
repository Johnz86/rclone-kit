"""Unit tests for `scripts/native/verify_submodule_pin.py`.

Uses `monkeypatch` on `subprocess.run` so these tests exercise the pin-
verification logic without a real git/network call; the real fetch is
proven separately by running the script against the live submodule pin.
"""

import subprocess
from pathlib import Path

import pytest
import verify_submodule_pin


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _submodule_dir(tmp_path: Path) -> Path:
    submodule_dir = tmp_path / "native" / "rclone"
    (submodule_dir / ".git").mkdir(parents=True)
    return submodule_dir


def test_verify_fork_pin_succeeds_when_commit_is_fetchable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    submodule_dir = _submodule_dir(tmp_path)

    def fake_run(command: list[str], **_kwargs: object) -> _FakeCompletedProcess:
        if command[:2] == ["git", "rev-parse"]:
            return _FakeCompletedProcess(returncode=0, stdout="abc123\n")
        assert command[:2] == ["git", "fetch"]
        assert command[-1] == "abc123"
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    commit = verify_submodule_pin.verify_fork_pin(submodule_dir, "https://example.com/fork.git")

    assert commit == "abc123"


def test_verify_fork_pin_raises_when_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    submodule_dir = _submodule_dir(tmp_path)

    def fake_run(command: list[str], **_kwargs: object) -> _FakeCompletedProcess:
        if command[:2] == ["git", "rev-parse"]:
            return _FakeCompletedProcess(returncode=0, stdout="abc123\n")
        return _FakeCompletedProcess(returncode=1, stderr="couldn't find remote ref abc123")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(verify_submodule_pin.SubmodulePinError, match="abc123"):
        verify_submodule_pin.verify_fork_pin(submodule_dir, "https://example.com/fork.git")


def test_verify_fork_pin_raises_when_submodule_not_initialized(tmp_path: Path) -> None:
    uninitialized_dir = tmp_path / "native" / "rclone"
    uninitialized_dir.mkdir(parents=True)

    with pytest.raises(verify_submodule_pin.SubmodulePinError, match="not an initialized"):
        verify_submodule_pin.verify_fork_pin(uninitialized_dir, "https://example.com/fork.git")
