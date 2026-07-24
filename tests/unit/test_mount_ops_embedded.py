"""Unit tests for the embedded RC-backed mount operations (CLI-to-C-ABI
migration ledger rows R01 `mount`, R02 `mount_s3`), driven by a fake
`RcMountClient`. Native-DLL parity is covered by
`tests/native/test_mount_ops_embedded_integration.py`.
"""

from pathlib import Path

import pytest

from rclone_kit.exceptions import UnsupportedEmbeddedOperationError
from rclone_kit.mount_handle import MountHandle
from rclone_kit.operations.mount_ops_embedded import (
    fetch_mount_embedded,
    fetch_s3_mount_embedded,
)
from rclone_kit.rc.mount import MountRef
from rclone_kit.types import ModTimeStrategy


class FakeMountClient:
    def __init__(self) -> None:
        self.mount_calls: list[tuple[str, str, dict, dict, dict]] = []
        self.next_mount_point: str | None = None

    def mount(self, fs: str, mount_point: str, *, vfs_opt=None, mount_opt=None, config=None):
        self.mount_calls.append(
            (fs, mount_point, dict(vfs_opt or {}), dict(mount_opt or {}), dict(config or {}))
        )
        return MountRef(mount_point=self.next_mount_point or mount_point)

    def unmount(self, mount_point: str) -> None:
        del mount_point


class TestFetchMountEmbedded:
    def test_defaults_are_read_only_with_links_and_full_cache(self) -> None:
        client = FakeMountClient()

        handle = fetch_mount_embedded(client, "remote:base", Path("/mnt/tmp"))

        assert isinstance(handle, MountHandle)
        fs, mount_point, vfs_opt, mount_opt, config = client.mount_calls[0]
        assert fs == "remote:base"
        assert mount_point == str(Path("/mnt/tmp"))
        assert vfs_opt == {"ReadOnly": True, "Links": True, "CacheMode": "full"}
        assert mount_opt == {}
        assert config == {}

    def test_allow_writes_clears_read_only(self) -> None:
        client = FakeMountClient()

        fetch_mount_embedded(client, "remote:base", Path("/mnt/tmp"), allow_writes=True)

        _fs, _mp, vfs_opt, _mo, _cfg = client.mount_calls[0]
        assert vfs_opt["ReadOnly"] is False

    def test_use_links_false_disables_links(self) -> None:
        client = FakeMountClient()

        fetch_mount_embedded(client, "remote:base", Path("/mnt/tmp"), use_links=False)

        _fs, _mp, vfs_opt, _mo, _cfg = client.mount_calls[0]
        assert vfs_opt["Links"] is False

    def test_vfs_cache_mode_override(self) -> None:
        client = FakeMountClient()

        fetch_mount_embedded(client, "remote:base", Path("/mnt/tmp"), vfs_cache_mode="minimal")

        _fs, _mp, vfs_opt, _mo, _cfg = client.mount_calls[0]
        assert vfs_opt["CacheMode"] == "minimal"

    def test_transfers_sets_flat_config(self) -> None:
        client = FakeMountClient()

        fetch_mount_embedded(client, "remote:base", Path("/mnt/tmp"), transfers=8)

        _fs, _mp, _vo, _mo, config = client.mount_calls[0]
        assert config == {"transfers": 8}

    def test_returned_mount_path_reflects_actual_mount_point(self) -> None:
        client = FakeMountClient()
        client.next_mount_point = "Z:"

        handle = fetch_mount_embedded(client, "remote:base", Path("*"))

        assert handle.mount_path == Path("Z:")

    def test_other_args_raises(self) -> None:
        client = FakeMountClient()

        with pytest.raises(UnsupportedEmbeddedOperationError):
            fetch_mount_embedded(
                client, "remote:base", Path("/mnt/tmp"), other_args=["--allow-other"]
            )

    def test_cache_dir_raises(self) -> None:
        client = FakeMountClient()

        with pytest.raises(UnsupportedEmbeddedOperationError):
            fetch_mount_embedded(client, "remote:base", Path("/mnt/tmp"), cache_dir=Path("/cache"))


class TestFetchS3MountEmbedded:
    def test_defaults_match_launch_s3_mount(self) -> None:
        client = FakeMountClient()

        fetch_s3_mount_embedded(client, "remote:base", Path("/mnt/tmp"))

        _fs, _mp, vfs_opt, mount_opt, config = client.mount_calls[0]
        assert vfs_opt == {
            "ReadOnly": True,
            "CacheMode": "full",
            "DirCacheTime": "1h",
            "CacheMaxSize": "100M",
            "Refresh": True,
            "ChunkStreams": 16,
            "ChunkSize": "4M",
            "FastFingerprint": True,
        }
        assert mount_opt == {"AttrTimeout": "1h"}
        assert config == {"use_server_modtime": True, "transfers": 128}

    def test_vfs_disk_space_total_size_maps_to_cache_max_size_not_disk_space_total_size(
        self,
    ) -> None:
        """Bug-for-bug parity with `launch_s3_mount`: the Python parameter
        `vfs_disk_space_total_size` sets `--vfs-cache-max-size`
        (`vfsOpt.CacheMaxSize`), not the similarly-named
        `vfsOpt.DiskSpaceTotalSize` field."""
        client = FakeMountClient()

        fetch_s3_mount_embedded(
            client, "remote:base", Path("/mnt/tmp"), vfs_disk_space_total_size="42M"
        )

        _fs, _mp, vfs_opt, _mo, _cfg = client.mount_calls[0]
        assert vfs_opt["CacheMaxSize"] == "42M"
        assert "DiskSpaceTotalSize" not in vfs_opt

    def test_no_modtime_sets_vfs_opt_not_config(self) -> None:
        client = FakeMountClient()

        fetch_s3_mount_embedded(
            client, "remote:base", Path("/mnt/tmp"), modtime_strategy=ModTimeStrategy.NO_MODTIME
        )

        _fs, _mp, vfs_opt, _mo, config = client.mount_calls[0]
        assert vfs_opt["NoModTime"] is True
        assert "use_server_modtime" not in config

    def test_modtime_strategy_none_sets_neither(self) -> None:
        client = FakeMountClient()

        fetch_s3_mount_embedded(client, "remote:base", Path("/mnt/tmp"), modtime_strategy=None)

        _fs, _mp, vfs_opt, _mo, config = client.mount_calls[0]
        assert "NoModTime" not in vfs_opt
        assert "use_server_modtime" not in config

    def test_transfers_omitted_when_cache_mode_not_full_or_writes(self) -> None:
        client = FakeMountClient()

        fetch_s3_mount_embedded(
            client, "remote:base", Path("/mnt/tmp"), vfs_cache_mode="minimal", transfers=128
        )

        _fs, _mp, _vo, _mo, config = client.mount_calls[0]
        assert "transfers" not in config

    def test_transfers_included_when_cache_mode_writes(self) -> None:
        client = FakeMountClient()

        fetch_s3_mount_embedded(
            client, "remote:base", Path("/mnt/tmp"), vfs_cache_mode="writes", transfers=64
        )

        _fs, _mp, _vo, _mo, config = client.mount_calls[0]
        assert config["transfers"] == 64

    def test_other_args_raises(self) -> None:
        client = FakeMountClient()

        with pytest.raises(UnsupportedEmbeddedOperationError):
            fetch_s3_mount_embedded(
                client, "remote:base", Path("/mnt/tmp"), other_args=["--allow-other"]
            )
