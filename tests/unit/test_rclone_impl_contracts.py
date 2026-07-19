import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rclone_kit import rclone_impl as rclone_impl_module
from rclone_kit.rclone_impl import RcloneImpl
from rclone_kit.types import SizeResult


def _bare_rclone_impl() -> RcloneImpl:
    return object.__new__(RcloneImpl)


def test_size_files_empty_input_returns_empty_result() -> None:
    rclone = _bare_rclone_impl()

    result = rclone.size_files("remote:bucket", [])

    assert result == SizeResult(prefix="remote:bucket", total_size=0, file_sizes={})


def test_copy_files_empty_input_does_not_execute_rclone() -> None:
    rclone = _bare_rclone_impl()
    rclone._run = lambda *_args, **_kwargs: pytest.fail("rclone must not run")  # type: ignore[method-assign]

    assert rclone.copy_files("src:bucket", "dst:bucket", []) == []


def test_delete_files_empty_input_does_not_execute_rclone() -> None:
    rclone = _bare_rclone_impl()
    rclone._run = lambda *_args, **_kwargs: pytest.fail("rclone must not run")  # type: ignore[method-assign]

    result = rclone.delete_files([])

    assert result.ok
    assert result.completed[0].args == ["rclone", "delete", "--files-from", "[]"]


def test_copy_files_does_not_mutate_caller_arguments() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    rclone._run = run  # type: ignore[method-assign]
    other_args = ["--metadata"]

    result = rclone.copy_files("src:bucket", "dst:bucket", ["folder/file"], other_args=other_args)

    assert result[0].ok
    assert other_args == ["--metadata"]
    assert commands[0][-2:] == ["--metadata", "--s3-no-check-bucket"]


def test_mount_respects_explicit_false_for_links(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("rclone_kit.mount_util.ensure_mount_supported", lambda: None)
    monkeypatch.setattr("rclone_kit.mount_util.clean_mount", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("rclone_kit.mount_util.prepare_mount", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        rclone_impl_module,
        "Mount",
        SimpleNamespace,
    )
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []

    def launch(command: list[str], log: Path | None = None) -> object:
        del log
        commands.append(command)
        return object()

    rclone._launch_process = launch  # type: ignore[method-assign]

    rclone.mount(
        "remote:bucket",
        tmp_path / "mount",
        use_links=False,
        verbose=False,
    )

    assert "--links" not in commands[0]
