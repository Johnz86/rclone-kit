"""Unit tests for `rclone_kit.detail.transfer_ops`, extracted from
`RcloneImpl` as part of the public-facade-split roadmap phase. `RcloneImpl`
methods delegate to these functions unchanged, so these tests exercise the
actual logic; `test_rclone_impl_contracts.py` covers that the delegation
itself still works.
"""

import subprocess
from pathlib import Path

import pytest

from rclone_kit.detail.transfer_ops import (
    copy_between_remotes,
    copy_byte_range,
    copy_directory,
    copy_file_to,
    copy_files_partitioned,
    copy_tree,
    purge_dir,
)
from rclone_kit.rclone_impl import (
    FLAG_CHECKERS,
    FLAG_FILES_FROM,
    FLAG_LOW_LEVEL_RETRIES,
    FLAG_S3_NO_CHECK_BUCKET,
    FLAG_TRANSFERS,
    RcloneImpl,
)
from rclone_kit.remote import Remote


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


def test_copy_file_to_builds_expected_command_vector() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    copy_file_to(rclone, "src:bucket/a", "dst:bucket/a")

    assert commands == [
        ["copyto", "src:bucket/a", "dst:bucket/a", FLAG_S3_NO_CHECK_BUCKET, "--no-traverse"]
    ]


def test_copy_tree_builds_expected_command_vector_with_defaults() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    copy_tree(rclone, "src:bucket", "dst:bucket")

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


def test_purge_dir_builds_expected_command_vector() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    purge_dir(rclone, "remote:bucket")

    assert commands == [["purge", "remote:bucket"]]


def test_copy_byte_range_builds_expected_command_vector(tmp_path: Path) -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    copy_byte_range(rclone, "src:bucket/a", offset=10, length=20, outfile=tmp_path / "out.bin")

    assert commands == [["cat", "--offset", "10", "--count", "20", "src:bucket/a"]]


def test_copy_directory_builds_expected_command_vector() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    copy_directory(rclone, "src:bucket", "dst:bucket")

    assert commands == [["copy", "src:bucket", "dst:bucket", FLAG_S3_NO_CHECK_BUCKET]]


def test_copy_between_remotes_builds_expected_command_vector() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)
    src = Remote(name="src", rclone=rclone)
    dst = Remote(name="dst", rclone=rclone)

    copy_between_remotes(rclone, src, dst)

    assert commands == [["copy", "src:", "dst:", FLAG_S3_NO_CHECK_BUCKET]]


def test_copy_files_partitioned_empty_input_does_not_execute_rclone() -> None:
    rclone = _bare_rclone_impl()
    rclone._run = lambda *_args, **_kwargs: pytest.fail("rclone must not run")

    assert copy_files_partitioned(rclone, "src:bucket", "dst:bucket", []) == []


def test_copy_files_partitioned_rejects_fully_qualified_file_paths() -> None:
    rclone = _bare_rclone_impl()
    rclone._run = lambda *_args, **_kwargs: pytest.fail("rclone must not run")

    with pytest.raises(ValueError, match="not allowed for copy_files"):
        copy_files_partitioned(rclone, "src:bucket", "dst:bucket", ["remote:bucket/a.txt"])


def test_copy_files_partitioned_builds_expected_command_vector() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    copy_files_partitioned(
        rclone,
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
    assert cmd[3] == FLAG_FILES_FROM
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


def test_copy_files_partitioned_partitions_across_multiple_workers() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []
    file_contents: dict[str, str] = {}

    def run(cmd: list[str], check: bool = False, capture=None):
        del check, capture
        commands.append(cmd)
        files_from_index = cmd.index(FLAG_FILES_FROM)
        files_from_path = Path(cmd[files_from_index + 1])
        file_contents[cmd[1]] = files_from_path.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    rclone._run = run

    result = copy_files_partitioned(
        rclone,
        "src:bucket",
        "dst:bucket",
        ["dirA/a.txt", "dirB/b.txt"],
        max_partition_workers=2,
    )

    assert len(result) == 2
    assert all(cp.ok for cp in result)
    src_paths = {cmd[1] for cmd in commands}
    assert src_paths == {"src:bucket/dirA", "src:bucket/dirB"}
    assert file_contents["src:bucket/dirA"] == "a.txt"
    assert file_contents["src:bucket/dirB"] == "b.txt"


def test_copy_files_partitioned_failure_raises_after_running_all_partitions() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []

    def run(cmd: list[str], check: bool = False, capture=None):
        del check, capture
        commands.append(cmd)
        returncode = 1 if cmd[1] == "src:bucket/dirA" else 0
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="boom")

    rclone._run = run

    with pytest.raises(ValueError, match="boom"):
        copy_files_partitioned(
            rclone,
            "src:bucket",
            "dst:bucket",
            ["dirA/a.txt", "dirB/b.txt"],
            max_partition_workers=2,
        )

    assert len(commands) == 2


def test_copy_files_partitioned_fast_list_warns() -> None:
    rclone = _bare_rclone_impl()
    rclone._run = _recording_run([])

    with pytest.warns(UserWarning, match="fast-list"):
        copy_files_partitioned(
            rclone, "src:bucket", "dst:bucket", ["a.txt"], other_args=["--fast-list"]
        )
