import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rclone_kit import rclone_impl as rclone_impl_module
from rclone_kit.dir_listing import DirListing
from rclone_kit.exceptions import RcloneCommandError
from rclone_kit.group_files import group_files
from rclone_kit.process import Process
from rclone_kit.rclone_impl import (
    FLAG_CHECKERS,
    FLAG_FILES_FROM,
    FLAG_LOW_LEVEL_RETRIES,
    FLAG_S3_NO_CHECK_BUCKET,
    FLAG_TRANSFERS,
    RcloneImpl,
)
from rclone_kit.types import SizeResult


def _bare_rclone_impl() -> RcloneImpl:
    return object.__new__(RcloneImpl)


def _recording_run(commands: list[list[str]]):
    def run(
        cmd: list[str],
        check: bool = False,
        capture: bool | Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return run


def test_stat_raises_file_not_found_for_missing_path() -> None:
    rclone = _bare_rclone_impl()
    rclone.ls = lambda *_args, **_kwargs: DirListing([])

    with pytest.raises(FileNotFoundError):
        rclone.stat("remote:bucket/missing.txt")


def test_read_bytes_raises_rclone_command_error_when_copy_fails() -> None:
    rclone = _bare_rclone_impl()

    def copy_to(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["rclone", "copyto"], stderr="boom")

    rclone.copy_to = copy_to

    with pytest.raises(RcloneCommandError):
        rclone.read_bytes("remote:bucket/missing.txt")


def test_config_show_raises_rclone_command_error_on_failed_command() -> None:
    rclone = _bare_rclone_impl()

    def run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["rclone", "config", "show"], stderr="boom")

    rclone._run = run

    with pytest.raises(RcloneCommandError):
        rclone.config_show()


def test_size_files_empty_input_returns_empty_result() -> None:
    rclone = _bare_rclone_impl()

    result = rclone.size_files("remote:bucket", [])

    assert result == SizeResult(prefix="remote:bucket", total_size=0, file_sizes={})


def test_copy_files_empty_input_does_not_execute_rclone() -> None:
    rclone = _bare_rclone_impl()
    rclone._run = lambda *_args, **_kwargs: pytest.fail("rclone must not run")

    assert rclone.copy_files("src:bucket", "dst:bucket", []) == []


def test_delete_files_empty_input_does_not_execute_rclone() -> None:
    rclone = _bare_rclone_impl()
    rclone._run = lambda *_args, **_kwargs: pytest.fail("rclone must not run")

    result = rclone.delete_files([])

    assert result.ok
    assert result.completed[0].args == ["rclone", "delete", "--files-from", "[]"]


def test_copy_files_does_not_mutate_caller_arguments() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []

    def run(
        cmd: list[str],
        check: bool = False,
        capture: bool | Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    rclone._run = run
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

    def launch(
        cmd: list[str],
        capture: bool | None = None,
        log: Path | None = None,
    ) -> Process:
        del capture, log
        commands.append(cmd)
        return object.__new__(Process)

    rclone._launch_process = launch

    rclone.mount(
        "remote:bucket",
        tmp_path / "mount",
        use_links=False,
        verbose=False,
    )

    assert "--links" not in commands[0]


def test_copy_to_builds_expected_command_vector() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    rclone.copy_to("src:bucket/a", "dst:bucket/a")

    assert commands == [
        ["copyto", "src:bucket/a", "dst:bucket/a", FLAG_S3_NO_CHECK_BUCKET, "--no-traverse"]
    ]


def test_copy_builds_expected_command_vector_with_defaults() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    rclone.copy("src:bucket", "dst:bucket")

    assert commands == [
        [
            "copy",
            "src:bucket",
            "dst:bucket",
            FLAG_CHECKERS,
            "1000",
            FLAG_TRANSFERS,
            "32",
            FLAG_LOW_LEVEL_RETRIES,
            "10",
            FLAG_S3_NO_CHECK_BUCKET,
        ]
    ]


def test_copy_files_builds_expected_command_vector() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    rclone.copy_files(
        "src:bucket",
        "dst:bucket",
        ["a.txt", "b.txt"],
        max_partition_workers=1,
    )

    assert len(commands) == 1
    cmd = commands[0]
    assert cmd[0] == "copy"
    assert cmd[1] == "src:bucket"
    assert cmd[2] == "dst:bucket"
    assert cmd[3] == "--files-from"
    assert cmd[5:] == [
        FLAG_CHECKERS,
        "1000",
        FLAG_TRANSFERS,
        "32",
        FLAG_LOW_LEVEL_RETRIES,
        "10",
        "--retries",
        "3",
        FLAG_S3_NO_CHECK_BUCKET,
    ]


def test_delete_files_builds_expected_command_vector() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)
    files = ["remote:bucket/a.txt", "remote:bucket/b.txt"]
    expected_groups = group_files(list(files))
    assert len(expected_groups) == 1
    expected_remote = next(iter(expected_groups))

    result = rclone.delete_files(files, max_partition_workers=1)

    assert result.ok
    assert len(commands) == 1
    cmd = commands[0]
    assert cmd[0] == "delete"
    assert cmd[1] == expected_remote
    assert cmd[2] == FLAG_FILES_FROM
    assert cmd[4:8] == [FLAG_CHECKERS, "1000", FLAG_TRANSFERS, "1000"]


def test_copy_bytes_builds_expected_command_vector(tmp_path: Path) -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    rclone.copy_bytes("src:bucket/a", offset=10, length=20, outfile=tmp_path / "out.bin")

    assert commands == [["cat", "--offset", "10", "--count", "20", "src:bucket/a"]]
