from __future__ import annotations

from pathlib import Path
from typing import Protocol

from rclone_kit.backend import RcloneBackend
from rclone_kit.command_flags import FLAG_TRANSFERS, FLAG_VFS_CACHE_MODE
from rclone_kit.convert import convert_to_str
from rclone_kit.dir import Dir
from rclone_kit.mount import Mount
from rclone_kit.mount_util import clean_mount, ensure_mount_supported, prepare_mount
from rclone_kit.remote import Remote
from rclone_kit.types import ModTimeStrategy
from rclone_kit.util import get_verbose


class MountAccess(Protocol):
    """High-level callback required by the S3 mount preset."""

    def mount(
        self,
        src: Remote | Dir | str,
        outdir: Path,
        allow_writes: bool | None = False,
        transfers: int | None = None,
        use_links: bool | None = None,
        vfs_cache_mode: str | None = None,
        verbose: bool | None = None,
        cache_dir: Path | None = None,
        cache_dir_delete_on_exit: bool | None = None,
        log: Path | None = None,
        other_args: list[str] | None = None,
    ) -> Mount: ...


def launch_mount(
    backend: RcloneBackend,
    src: Remote | Dir | str,
    outdir: Path,
    allow_writes: bool | None = False,
    transfers: int | None = None,
    use_links: bool | None = None,
    vfs_cache_mode: str | None = None,
    verbose: bool | None = None,
    cache_dir: Path | None = None,
    cache_dir_delete_on_exit: bool | None = None,
    log: Path | None = None,
    other_args: list[str] | None = None,
) -> Mount:
    """Mount a remote or directory to a local path.

    Raises `MountPrerequisiteError` if the current platform lacks the
    operating-system mount facility rclone's `mount` subcommand requires
    (WinFsp on Windows, FUSE on Linux).
    """
    ensure_mount_supported()
    allow_writes = False if allow_writes is None else allow_writes
    use_links = True if use_links is None else use_links
    verbose = get_verbose(verbose) or (log is not None)
    vfs_cache_mode = vfs_cache_mode or "full"
    clean_mount(outdir, verbose=verbose)
    prepare_mount(outdir, verbose=verbose)
    debug_fuse = log is not None
    src_str = convert_to_str(src)
    cmd_list: list[str] = ["mount", src_str, str(outdir)]
    if not allow_writes:
        cmd_list.append("--read-only")
    if use_links:
        cmd_list.append("--links")
    if vfs_cache_mode:
        cmd_list.append(FLAG_VFS_CACHE_MODE)
        cmd_list.append(vfs_cache_mode)
    if cache_dir:
        cmd_list.append("--cache-dir")
        cmd_list.append(str(cache_dir.absolute()))
    if transfers is not None:
        cmd_list.append(FLAG_TRANSFERS)
        cmd_list.append(str(transfers))
    if debug_fuse:
        cmd_list.append("--debug-fuse")
    if verbose:
        cmd_list.append("-vvvv")
    if other_args:
        cmd_list += other_args
    proc = backend.launch(tuple(cmd_list), log=log)
    mount_read_only = not allow_writes
    mount: Mount = Mount(
        src=src_str,
        mount_path=outdir,
        process=proc,
        read_only=mount_read_only,
        cache_dir=cache_dir,
        cache_dir_delete_on_exit=cache_dir_delete_on_exit,
    )
    return mount


def launch_s3_mount(
    access: MountAccess,
    url: str,
    outdir: Path,
    allow_writes: bool = False,
    vfs_cache_mode: str = "full",
    dir_cache_time: str | None = "1h",
    attribute_timeout: str | None = "1h",
    vfs_disk_space_total_size: str | None = "100M",
    transfers: int | None = 128,
    modtime_strategy: ModTimeStrategy | None = ModTimeStrategy.USE_SERVER_MODTIME,
    vfs_read_chunk_streams: int | None = 16,
    vfs_read_chunk_size: str | None = "4M",
    vfs_fast_fingerprint: bool = True,
    vfs_refresh: bool = True,
    other_args: list[str] | None = None,
) -> Mount:
    """Mount a remote or directory to a local path with S3-tuned VFS defaults."""
    other_args = other_args or []
    if modtime_strategy is not None:
        other_args.append(f"--{modtime_strategy.value}")
    if (vfs_cache_mode in {"full", "writes"}) and (
        transfers is not None and FLAG_TRANSFERS not in other_args
    ):
        other_args.append(FLAG_TRANSFERS)
        other_args.append(str(transfers))
    if dir_cache_time is not None and "--dir-cache-time" not in other_args:
        other_args.append("--dir-cache-time")
        other_args.append(dir_cache_time)
    if vfs_disk_space_total_size is not None and "--vfs-cache-max-size" not in other_args:
        other_args.append("--vfs-cache-max-size")
        other_args.append(vfs_disk_space_total_size)
    if vfs_refresh and "--vfs-refresh" not in other_args:
        other_args.append("--vfs-refresh")
    if attribute_timeout is not None and "--attr-timeout" not in other_args:
        other_args.append("--attr-timeout")
        other_args.append(attribute_timeout)
    if vfs_read_chunk_streams:
        other_args.append("--vfs-read-chunk-streams")
        other_args.append(str(vfs_read_chunk_streams))
    if vfs_read_chunk_size:
        other_args.append("--vfs-read-chunk-size")
        other_args.append(vfs_read_chunk_size)
    if vfs_fast_fingerprint:
        other_args.append("--vfs-fast-fingerprint")

    other_args = other_args if other_args else None
    return access.mount(
        url,
        outdir,
        allow_writes=allow_writes,
        vfs_cache_mode=vfs_cache_mode,
        other_args=other_args,
    )
