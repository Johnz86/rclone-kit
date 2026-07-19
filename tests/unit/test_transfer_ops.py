"""Unit tests for `rclone_kit.detail.transfer_ops`, extracted from
`RcloneImpl` as part of the public-facade-split roadmap phase. `RcloneImpl`
methods delegate to these functions unchanged, so these tests exercise the
actual logic; `test_rclone_impl_contracts.py` covers that the delegation
itself still works.
"""

import subprocess
from pathlib import Path

from rclone_kit.detail.transfer_ops import (
    copy_between_remotes,
    copy_byte_range,
    copy_directory,
    copy_file_to,
    copy_tree,
    purge_dir,
)
from rclone_kit.rclone_impl import (
    FLAG_CHECKERS,
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
