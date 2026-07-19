"""Unit tests for `rclone_kit.detail.mount_ops`, extracted from `RcloneImpl`
as part of the public-facade-split roadmap phase. `RcloneImpl.mount`/
`mount_s3` delegate to these functions unchanged.

`launch_s3_mount`'s VFS-flag-building logic (the part actually worth
testing) had no unit coverage before this - only a real, network-dependent
cloud test exercised it end-to-end.
"""

from pathlib import Path
from typing import Any, cast

from rclone_kit.detail.mount_ops import launch_s3_mount
from rclone_kit.mount import Mount
from rclone_kit.rclone_impl import RcloneImpl


def _rclone_recording_mount() -> tuple[RcloneImpl, dict[str, Any]]:
    rclone = object.__new__(RcloneImpl)
    captured: dict[str, Any] = {}

    def mount(*args: Any, **kwargs: Any) -> Mount:
        captured["url"] = args[0]
        captured["outdir"] = args[1]
        captured.update(kwargs)
        return cast(Mount, object())

    rclone.mount = mount
    return rclone, captured


def test_launch_s3_mount_builds_expected_other_args_with_defaults() -> None:
    rclone, captured = _rclone_recording_mount()

    launch_s3_mount(rclone, "remote:bucket", Path("/mnt/x"))

    assert captured["url"] == "remote:bucket"
    assert captured["outdir"] == Path("/mnt/x")
    assert captured["allow_writes"] is False
    assert captured["vfs_cache_mode"] == "full"
    assert captured["other_args"] == [
        "--use-server-modtime",
        "--transfers",
        "128",
        "--dir-cache-time",
        "1h",
        "--vfs-cache-max-size",
        "100M",
        "--vfs-refresh",
        "--attr-timeout",
        "1h",
        "--vfs-read-chunk-streams",
        "16",
        "--vfs-read-chunk-size",
        "4M",
        "--vfs-fast-fingerprint",
    ]


def test_launch_s3_mount_omits_transfers_when_vfs_cache_mode_is_off() -> None:
    rclone, captured = _rclone_recording_mount()

    launch_s3_mount(rclone, "remote:bucket", Path("/mnt/x"), vfs_cache_mode="off")

    assert "--transfers" not in captured["other_args"]


def test_launch_s3_mount_respects_none_disabled_options() -> None:
    rclone, captured = _rclone_recording_mount()

    launch_s3_mount(
        rclone,
        "remote:bucket",
        Path("/mnt/x"),
        vfs_cache_mode="off",
        modtime_strategy=None,
        dir_cache_time=None,
        vfs_disk_space_total_size=None,
        attribute_timeout=None,
        vfs_read_chunk_streams=None,
        vfs_read_chunk_size=None,
        vfs_fast_fingerprint=False,
        vfs_refresh=False,
    )

    assert captured["other_args"] is None


def test_launch_s3_mount_does_not_duplicate_flags_already_in_other_args() -> None:
    rclone, captured = _rclone_recording_mount()

    launch_s3_mount(
        rclone,
        "remote:bucket",
        Path("/mnt/x"),
        other_args=["--dir-cache-time", "30m"],
    )

    other_args = captured["other_args"]
    assert other_args.count("--dir-cache-time") == 1
    dir_cache_time_value = other_args[other_args.index("--dir-cache-time") + 1]
    assert dir_cache_time_value == "30m"
