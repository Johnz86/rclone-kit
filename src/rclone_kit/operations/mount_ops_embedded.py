"""Embedded RC-backed mount operations (CLI-to-C-ABI migration ledger rows
R01 `mount`, R02 `mount_s3`).

Both start a `mount/mount` instance via `RcMountClient` and wrap the result
in a `MountHandle`. `mount/mount`'s `vfsOpt`/`mountOpt` parameters are JSON
objects decoded with standard Go field names (`"CacheMode"`, `"ReadOnly"`,
...) rather than this project's usual underscored `config:`-tag flat params
- see `rc/mount.py`'s module docstring - so these functions build those
objects directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rclone_kit.mount_handle import MountHandle
from rclone_kit.types import ModTimeStrategy

if TYPE_CHECKING:
    from pathlib import Path

    from rclone_kit.rc.mount import RcMountClient

_S3_VFS_CACHE_MODE = "full"
_S3_DIR_CACHE_TIME = "1h"
_S3_ATTRIBUTE_TIMEOUT = "1h"
_S3_VFS_DISK_SPACE_TOTAL_SIZE = "100M"
_S3_TRANSFERS = 128
_S3_VFS_READ_CHUNK_STREAMS = 16
_S3_VFS_READ_CHUNK_SIZE = "4M"


def fetch_mount_embedded(
    mount_client: RcMountClient,
    src: str,
    outdir: Path,
    allow_writes: bool | None = False,
    transfers: int | None = None,
    use_links: bool | None = None,
    vfs_cache_mode: str | None = None,
) -> MountHandle:
    """Mount `src` at `outdir` through `mount/mount`.

    Defaults: `allow_writes=False` unless given, `use_links=True` unless
    given, `vfs_cache_mode="full"` unless given.
    """
    allow_writes = False if allow_writes is None else allow_writes
    use_links = True if use_links is None else use_links
    vfs_cache_mode = vfs_cache_mode or "full"
    vfs_opt: dict[str, object] = {
        "ReadOnly": not allow_writes,
        "Links": use_links,
        "CacheMode": vfs_cache_mode,
    }
    config: dict[str, object] = {}
    if transfers is not None:
        config["transfers"] = transfers
    ref = mount_client.mount(src, str(outdir), vfs_opt=vfs_opt, config=config)
    return MountHandle(mount_client, ref.mount_point)


def fetch_s3_mount_embedded(
    mount_client: RcMountClient,
    url: str,
    outdir: Path,
    allow_writes: bool = False,
    vfs_cache_mode: str = _S3_VFS_CACHE_MODE,
    dir_cache_time: str | None = _S3_DIR_CACHE_TIME,
    attribute_timeout: str | None = _S3_ATTRIBUTE_TIMEOUT,
    vfs_disk_space_total_size: str | None = _S3_VFS_DISK_SPACE_TOTAL_SIZE,
    transfers: int | None = _S3_TRANSFERS,
    modtime_strategy: ModTimeStrategy | None = ModTimeStrategy.USE_SERVER_MODTIME,
    vfs_read_chunk_streams: int | None = _S3_VFS_READ_CHUNK_STREAMS,
    vfs_read_chunk_size: str | None = _S3_VFS_READ_CHUNK_SIZE,
    vfs_fast_fingerprint: bool = True,
    vfs_refresh: bool = True,
) -> MountHandle:
    """Mount `url` at `outdir` with S3-tuned VFS defaults.

    Builds `vfsOpt`/`mountOpt`/flat-config fields directly.
    `vfs_disk_space_total_size` sets `vfsOpt.CacheMaxSize` (not the
    similarly-named `vfsOpt.DiskSpaceTotalSize` field) - a deliberately
    preserved historical naming quirk. `modtime_strategy` is split across
    two different underlying option structs depending on its value:
    `USE_SERVER_MODTIME` is a global `_config` option
    (`fs.ConfigInfo.UseServerModTime`), while `NO_MODTIME` is a `vfsOpt`
    field (`vfscommon.Options.NoModTime`).
    """
    vfs_opt: dict[str, object] = {
        "ReadOnly": not allow_writes,
        "CacheMode": vfs_cache_mode,
    }
    mount_opt: dict[str, object] = {}
    config: dict[str, object] = {}
    if modtime_strategy is ModTimeStrategy.USE_SERVER_MODTIME:
        config["use_server_modtime"] = True
    elif modtime_strategy is ModTimeStrategy.NO_MODTIME:
        vfs_opt["NoModTime"] = True
    if vfs_cache_mode in {"full", "writes"} and transfers is not None:
        config["transfers"] = transfers
    if dir_cache_time is not None:
        vfs_opt["DirCacheTime"] = dir_cache_time
    if vfs_disk_space_total_size is not None:
        vfs_opt["CacheMaxSize"] = vfs_disk_space_total_size
    if vfs_refresh:
        vfs_opt["Refresh"] = True
    if attribute_timeout is not None:
        mount_opt["AttrTimeout"] = attribute_timeout
    if vfs_read_chunk_streams:
        vfs_opt["ChunkStreams"] = vfs_read_chunk_streams
    if vfs_read_chunk_size:
        vfs_opt["ChunkSize"] = vfs_read_chunk_size
    if vfs_fast_fingerprint:
        vfs_opt["FastFingerprint"] = True
    ref = mount_client.mount(url, str(outdir), vfs_opt=vfs_opt, mount_opt=mount_opt, config=config)
    return MountHandle(mount_client, ref.mount_point)
