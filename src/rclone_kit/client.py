"""Rclone implementation providing the public operation surface."""

from __future__ import annotations

import logging
import subprocess
import time
import warnings
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from rclone_kit.backend import CliRcloneBackend, RcloneBackend
from rclone_kit.command_flags import FLAG_FAST_LIST
from rclone_kit.completed_process import CompletedProcess
from rclone_kit.config import Config
from rclone_kit.config_discovery import find_conf_file
from rclone_kit.detail.config_ops import (
    check_is_s3,
    fetch_config_paths,
    fetch_config_show,
    fetch_s3_credentials,
    obscure_password,
)
from rclone_kit.detail.copy_file_parts_resumable import copy_file_parts_resumable
from rclone_kit.detail.listing_ops import (
    check_exists,
    check_is_synced,
    fetch_listremotes,
    fetch_ls,
    fetch_modtime,
    fetch_modtime_dt,
    fetch_size_file,
    fetch_size_files,
    fetch_stat,
    print_contents,
    stream_diff,
)
from rclone_kit.detail.mount_ops import launch_mount, launch_s3_mount
from rclone_kit.detail.serve_ops import launch_http_server, launch_webdav_server
from rclone_kit.detail.transfer_ops import (
    copy_between_remotes,
    copy_byte_range,
    copy_directory,
    copy_file_to,
    copy_files_partitioned,
    copy_tree,
    delete_files_partitioned,
    purge_dir,
)
from rclone_kit.detail.walk import walk
from rclone_kit.diff import DiffItem, DiffOption
from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.exceptions import RcloneCommandError
from rclone_kit.file import File
from rclone_kit.file_stream import FilesStream
from rclone_kit.fs.filesystem import FSPath, RemoteFS
from rclone_kit.http_server import HttpServer
from rclone_kit.mount import Mount
from rclone_kit.optional_dependency import MissingOptionalDependencyError
from rclone_kit.process import Process
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath
from rclone_kit.s3.types import S3UploadTarget
from rclone_kit.scan_missing_folders import scan_missing_folders
from rclone_kit.types import (
    ListingOption,
    ModTimeStrategy,
    Order,
    PartInfo,
    S3PathInfo,
    SizeResult,
    SizeSuffix,
)
from rclone_kit.util import (
    get_rclone_exe,
    get_verbose,
    to_path,
    upgrade_rclone,
)

if TYPE_CHECKING:
    from rclone_kit.s3.api import S3Client
    from rclone_kit.s3.types import S3Credentials

logger = logging.getLogger(__name__)


def _to_rclone_conf(config: Config | Path | None) -> Config:
    if config is None:
        return Config(None)
    elif isinstance(config, Path):
        content = config.read_text(encoding="utf-8")
        return Config(content)
    else:
        return config


class Rclone:
    """Curated high-level API for rclone operations."""

    @staticmethod
    def upgrade_rclone() -> Path:
        """Download and install the verified rclone executable."""
        return upgrade_rclone()

    @staticmethod
    def find_rclone_conf() -> Path | None:
        """Find the rclone configuration file using standard discovery."""
        return find_conf_file()

    def __init__(
        self,
        rclone_conf: Path | Config | None,
        rclone_exe: Path | None = None,
        *,
        backend: RcloneBackend | None = None,
    ) -> None:
        if isinstance(rclone_conf, Path) and not rclone_conf.exists():
            raise ValueError(f"Rclone config file not found: {rclone_conf}")
        if backend is None:
            resolved_executable = get_rclone_exe(rclone_exe)
            if rclone_conf is None:
                maybe_path = find_conf_file(rclone_exe=resolved_executable)
                if not isinstance(maybe_path, Path):
                    warnings.warn("Rclone config file not found", stacklevel=2)
                rclone_conf = _to_rclone_conf(maybe_path)
            backend = CliRcloneBackend(rclone_conf, resolved_executable)

        self._backend = backend
        self.config: Config = _to_rclone_conf(rclone_conf)

    def _run(
        self, cmd: list[str], check: bool = False, capture: bool | Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self._backend.run(tuple(cmd), check=check, capture=capture)

    def _launch_process(
        self, cmd: list[str], capture: bool | None = None, log: Path | None = None
    ) -> Process:
        return self._backend.launch(tuple(cmd), capture=capture, log=log)

    def _get_tmp_mount_dir(self) -> Path:
        return Path("tmp_mnts")

    def _get_cache_dir(self) -> Path:
        return Path("cache")

    def webgui(self, other_args: list[str] | None = None) -> Process:
        """Launch the Rclone web GUI."""
        cmd = ["rcd", "--rc-web-gui"]
        if other_args:
            cmd += other_args
        return self._launch_process(cmd, capture=False)

    def filesystem(self, src: str) -> RemoteFS:
        return RemoteFS(self, src)

    def cwd(self, src: str) -> FSPath:
        return self.filesystem(src).cwd()

    def launch_server(
        self,
        addr: str,
        user: str | None = None,
        password: str | None = None,
        other_args: list[str] | None = None,
    ) -> Process:
        """Launch the Rclone server so it can receive commands"""
        cmd = ["rcd"]
        if addr is not None:
            cmd += ["--rc-addr", addr]
        if user is not None:
            cmd += ["--rc-user", user]
        if password is not None:
            cmd += ["--rc-pass", password]
        if other_args:
            cmd += other_args
        out = self._launch_process(cmd, capture=False)
        time.sleep(1)
        return out

    def remote_control(
        self,
        addr: str,
        user: str | None = None,
        password: str | None = None,
        capture: bool | None = None,
        other_args: list[str] | None = None,
    ) -> CompletedProcess:
        cmd = ["rc"]
        if addr:
            cmd += ["--rc-addr", addr]
        if user is not None:
            cmd += ["--rc-user", user]
        if password is not None:
            cmd += ["--rc-pass", password]
        if other_args:
            cmd += other_args
        cp = self._run(cmd, capture=capture)
        return CompletedProcess.from_subprocess(cp)

    def obscure(self, password: str) -> str:
        """Obscure a password for use in rclone config files."""
        return obscure_password(self._backend, password)

    def ls_stream(
        self,
        src: str,
        max_depth: int = -1,
        fast_list: bool = False,
    ) -> FilesStream:
        """
        List files in the given path

        Args:
            src: Remote path to list
            max_depth: Maximum recursion depth (-1 for unlimited)
            fast_list: Use fast list (only use when getting THE entire data repository from the root/bucket, or it's small)
        """
        cmd = ["lsjson", src, "--files-only"]
        recurse = max_depth < 0 or max_depth > 1
        if recurse:
            cmd.append("-R")
            if max_depth > 1:
                cmd += ["--max-depth", str(max_depth)]
        if fast_list:
            cmd.append(FLAG_FAST_LIST)
        streamer = FilesStream(src, self._launch_process(cmd, capture=True))
        return streamer

    def save_to_db(
        self,
        src: str,
        db_url: str,
        max_depth: int = -1,
        fast_list: bool = False,
    ) -> None:
        """
        Save files to a database (sqlite, mysql, postgres)

        Args:
            src: Remote path to list, this will be used to populate an entire table, so always use the root-most path.
            db_url: Database URL, like sqlite:///data.db or mysql://user:pass@localhost/db or postgres://user:pass@localhost/db
            max_depth: Maximum depth to traverse (-1 for unlimited)
            fast_list: Use fast list (only use when getting THE entire data repository from the root/bucket)

        """
        try:
            from rclone_kit.db import DB
        except ModuleNotFoundError as error:
            raise MissingOptionalDependencyError(
                "Database operations", "database", "sqlmodel"
            ) from error

        db = DB(db_url)
        with self.ls_stream(src, max_depth, fast_list) as stream:
            for page in stream.files_paged(page_size=10000):
                db.add_files(page)

    def ls(
        self,
        src: Dir | Remote | str | None = None,
        max_depth: int | None = None,
        glob: str | None = None,
        order: Order = Order.NORMAL,
        listing_option: ListingOption = ListingOption.ALL,
    ) -> DirListing:
        """List files in the given path.

        Args:
            src: Remote path or Remote object to list
            max_depth: Maximum recursion depth (0 means no recursion)

        Returns:
            List of File objects found at the path
        """
        return fetch_ls(
            self._backend,
            self,
            src,
            max_depth=max_depth,
            glob=glob,
            order=order,
            listing_option=listing_option,
        )

    def print(self, src: str) -> None:
        """Print the contents of a file."""
        print_contents(self, src)

    def stat(self, src: str) -> File:
        """Get the status of a file or directory.

        Raises FileNotFoundError if `src` does not exist.
        """
        return fetch_stat(self, src)

    def modtime(self, src: str) -> str:
        """Get the modification time of a file or directory."""
        return fetch_modtime(self, src)

    def modtime_dt(self, src: str) -> datetime:
        """Get the modification time of a file or directory."""
        return fetch_modtime_dt(self, src)

    def listremotes(self) -> list[Remote]:
        return fetch_listremotes(self._backend, self)

    def diff(
        self,
        src: str,
        dst: str,
        min_size: (str | None) = None,
        max_size: (str | None) = None,
        diff_option: DiffOption = DiffOption.COMBINED,
        fast_list: bool = True,
        size_only: bool | None = None,
        checkers: int | None = None,
        other_args: list[str] | None = None,
    ) -> Generator[DiffItem]:
        """Be extra careful with the src and dst values. If you are off by one
        parent directory, you will get a huge amount of false diffs."""
        yield from stream_diff(
            self._backend,
            src,
            dst,
            min_size=min_size,
            max_size=max_size,
            diff_option=diff_option,
            fast_list=fast_list,
            size_only=size_only,
            checkers=checkers,
            other_args=other_args,
        )

    def walk(
        self,
        src: Dir | Remote | str,
        max_depth: int = -1,
        breadth_first: bool = True,
        order: Order = Order.NORMAL,
    ) -> Generator[DirListing]:
        """Walk through the given path recursively.

        Args:
            src: Remote path or Remote object to walk through
            max_depth: Maximum depth to traverse (-1 for unlimited)

        Yields:
            DirListing: Directory listing for each directory encountered
        """
        dir_obj: Dir
        if isinstance(src, Dir):
            remote = src.remote
            rpath = RPath(
                remote=remote,
                path=src.path.path,
                name=src.path.name,
                size=0,
                mime_type="inode/directory",
                mod_time="",
                is_dir=True,
            )
            rpath.set_rclone(self)
            dir_obj = Dir(rpath)
        elif isinstance(src, str):
            dir_obj = Dir(to_path(src, self))
        elif isinstance(src, Remote):
            dir_obj = Dir(src)
        else:
            raise TypeError(f"Invalid type for path: {type(src)}")

        yield from walk(dir_obj, max_depth=max_depth, breadth_first=breadth_first, order=order)

    def scan_missing_folders(
        self,
        src: Dir | Remote | str,
        dst: Dir | Remote | str,
        max_depth: int = -1,
        order: Order = Order.NORMAL,
    ) -> Generator[Dir]:
        """Yield every directory present under `src` that is missing under
        the corresponding relative path in `dst`.

        A folder found missing is yielded once for itself; if it has a
        subtree, every descendant directory is yielded too (walked via
        `detail.walk.walk_runner_depth_first`, since a whole missing
        subtree needs no further src/dst comparison - none of it exists on
        the `dst` side by definition). Folders present under `src` and
        `dst` at a given relative path are recursed into, in case they
        diverge further down.

        Args:
            src: Source directory or Remote to walk through
            dst: Destination directory or Remote to walk through
            max_depth: Maximum depth to traverse (-1 for unlimited)

        Yields:
            Dir: each directory present under `src` but missing under `dst`
        """
        src_dir = Dir(to_path(src, self))
        dst_dir = Dir(to_path(dst, self))
        yield from scan_missing_folders(src=src_dir, dst=dst_dir, max_depth=max_depth, order=order)

    def cleanup(self, src: str, other_args: list[str] | None = None) -> CompletedProcess:
        """Cleanup any resources used by the Rclone instance."""

        cmd = ["cleanup", src]
        if other_args:
            cmd += other_args
        out = self._run(cmd)
        return CompletedProcess.from_subprocess(out)

    def get_verbose(self) -> bool:
        return get_verbose(None)

    def copy_to(
        self,
        src: File | str,
        dst: File | str,
        check: bool | None = None,
        verbose: bool | None = None,
        other_args: list[str] | None = None,
    ) -> CompletedProcess:
        """Copy one file from source to destination.

        Warning - slow.

        """
        return copy_file_to(
            self._backend,
            src,
            dst,
            check=check,
            verbose=verbose,
            other_args=other_args,
        )

    def copy_files(
        self,
        src: str,
        dst: str,
        files: list[str] | Path,
        check: bool | None = None,
        max_backlog: int | None = None,
        verbose: bool | None = None,
        checkers: int | None = None,
        transfers: int | None = None,
        low_level_retries: int | None = None,
        retries: int | None = None,
        retries_sleep: str | None = None,
        metadata: bool | None = None,
        timeout: str | None = None,
        max_partition_workers: int | None = None,
        multi_thread_streams: int | None = None,
        other_args: list[str] | None = None,
    ) -> list[CompletedProcess]:
        """Copy multiple files from source to destination.

        Args:
            payload: Dictionary of source and destination file paths
        """
        return copy_files_partitioned(
            self._backend,
            src,
            dst,
            files,
            check=check,
            max_backlog=max_backlog,
            verbose=verbose,
            checkers=checkers,
            transfers=transfers,
            low_level_retries=low_level_retries,
            retries=retries,
            retries_sleep=retries_sleep,
            metadata=metadata,
            timeout=timeout,
            max_partition_workers=max_partition_workers,
            multi_thread_streams=multi_thread_streams,
            other_args=other_args,
        )

    def copy(
        self,
        src: Dir | str,
        dst: Dir | str,
        check: bool | None = None,
        transfers: int | None = None,
        checkers: int | None = None,
        multi_thread_streams: int | None = None,
        low_level_retries: int | None = None,
        retries: int | None = None,
        other_args: list[str] | None = None,
    ) -> CompletedProcess:
        """Copy files from source to destination.

        Args:
            src: Source directory
            dst: Destination directory
        """
        return copy_tree(
            self._backend,
            src,
            dst,
            check=check,
            transfers=transfers,
            checkers=checkers,
            multi_thread_streams=multi_thread_streams,
            low_level_retries=low_level_retries,
            retries=retries,
            other_args=other_args,
        )

    def purge(self, src: Dir | str) -> CompletedProcess:
        """Purge a directory"""
        return purge_dir(self._backend, src)

    def delete_files(
        self,
        files: str | File | list[str] | list[File],
        check: bool | None = None,
        rmdirs=False,
        verbose: bool | None = None,
        max_partition_workers: int | None = None,
        other_args: list[str] | None = None,
    ) -> CompletedProcess:
        """Delete a directory"""
        return delete_files_partitioned(
            self._backend,
            files,
            check=check,
            rmdirs=rmdirs,
            verbose=verbose,
            max_partition_workers=max_partition_workers,
            other_args=other_args,
        )

    def exists(self, src: Dir | Remote | str | File) -> bool:
        """Check if a file or directory exists."""
        return check_exists(self, src)

    def is_synced(self, src: str | Dir, dst: str | Dir) -> bool:
        """Check if two directories are in sync."""
        return check_is_synced(self._backend, src, dst)

    def _s3_client(self, src: str, verbose: bool | None = None) -> S3Client:
        """Get an S3 client."""
        try:
            from rclone_kit.s3.api import S3Client
        except ModuleNotFoundError as error:
            raise MissingOptionalDependencyError("S3 operations", "s3", "boto3") from error

        verbose = get_verbose(verbose)
        s3_creds = self.get_s3_credentials(remote=src, verbose=verbose)
        s3_client = S3Client(s3_creds=s3_creds, verbose=verbose)
        return s3_client

    def copy_file_s3(
        self,
        src: Path,
        dst: str,
        verbose: bool | None = None,
    ) -> None:
        """Copy a file to S3.

        Raises ValueError if `dst` is not an S3 remote.
        """
        if not self.is_s3(dst):
            raise ValueError(f"Destination is not an S3 remote: {dst}")
        s3_client = self._s3_client(dst, verbose=verbose)

        path_info: S3PathInfo = S3PathInfo.from_str(dst)
        target: S3UploadTarget = S3UploadTarget(
            src_file=src,
            src_file_size=src.stat().st_size,
            bucket_name=path_info.bucket,
            s3_key=path_info.key,
        )
        s3_client.upload_file(target=target)

    def is_s3(self, dst: str) -> bool:
        """Check if a remote is an S3 remote."""
        return check_is_s3(self.config, dst)

    def copy_file_s3_resumable(
        self,
        src: str,
        dst: str,
        part_infos: list[PartInfo] | None = None,
        upload_threads: int = 8,
        merge_threads: int = 4,
    ) -> None:
        """Copy parts of a file from source to destination."""
        if dst.endswith("/"):
            dst = dst[:-1]
        dst_dir = f"{dst}-parts"

        copy_file_parts_resumable(
            access=self,
            src=src,
            dst_dir=dst_dir,
            part_infos=part_infos,
            upload_threads=upload_threads,
            merge_threads=merge_threads,
        )

    def write_text(
        self,
        text: str,
        dst: str,
    ) -> None:
        """Write text to a file."""
        self.write_bytes(data=text.encode("utf-8"), dst=dst)

    def write_bytes(
        self,
        data: bytes,
        dst: str,
    ) -> None:
        """Write bytes to a file.

        Raises RcloneCommandError if the underlying rclone command fails.
        """
        with TemporaryDirectory() as tmpdir:
            tmpfile = Path(tmpdir) / "file.bin"
            tmpfile.write_bytes(data)
            if self.is_s3(dst):
                self.copy_file_s3(tmpfile, dst)
                return

            try:
                self.copy_to(str(tmpfile), dst, check=True)
            except subprocess.CalledProcessError as error:
                raise RcloneCommandError("copyto", error.stderr or "", error) from error

    def read_bytes(self, src: str) -> bytes:
        """Read bytes from a file.

        Raises RcloneCommandError if the underlying rclone command fails
        or if rclone reports success without producing an output file.
        """
        with TemporaryDirectory() as tmpdir:
            tmpfile = Path(tmpdir) / "file.bin"
            try:
                self.copy_to(src, str(tmpfile), check=True)
            except subprocess.CalledProcessError as error:
                raise RcloneCommandError("copyto", error.stderr or "", error) from error

            if not tmpfile.exists():
                raise RcloneCommandError(
                    "copyto", "", FileNotFoundError(f"{src} produced no output file")
                )
            return tmpfile.read_bytes()

    def read_text(self, src: str) -> str:
        """Read text from a file."""
        return self.read_bytes(src).decode("utf-8")

    def size_file(self, src: str) -> SizeSuffix:
        """Get the size of a file or directory.

        Raises FileNotFoundError if no file matches `src`, or ValueError
        if more than one file matches.
        """
        return fetch_size_file(self, src)

    def get_s3_credentials(self, remote: str, verbose: bool | None = None) -> S3Credentials:
        return fetch_s3_credentials(self.config, remote, verbose=verbose)

    def copy_bytes(
        self,
        src: str,
        offset: int | SizeSuffix,
        length: int | SizeSuffix,
        outfile: Path,
        other_args: list[str] | None = None,
    ) -> None:
        """Copy a slice of bytes from the src file to outfile.

        Raises RcloneCommandError if the underlying rclone command fails.
        """
        copy_byte_range(self._backend, src, offset, length, outfile, other_args=other_args)

    def copy_dir(
        self, src: str | Dir, dst: str | Dir, args: list[str] | None = None
    ) -> CompletedProcess:
        """Copy a directory from source to destination."""
        return copy_directory(self._backend, src, dst, args=args)

    def copy_remote(
        self, src: Remote, dst: Remote, args: list[str] | None = None
    ) -> CompletedProcess:
        """Copy a remote to another remote."""
        return copy_between_remotes(self._backend, src, dst, args=args)

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
    ) -> Mount:
        """Mount a remote or directory to a local path.

        Args:
            src: Remote or directory to mount
            outdir: Local path to mount to

        Returns:
            CompletedProcess from the mount command execution

        Raises:
            subprocess.CalledProcessError: If the mount operation fails
            MountPrerequisiteError: If the current platform lacks the
                operating-system mount facility rclone's `mount` subcommand
                requires (WinFsp on Windows, FUSE on Linux)
        """
        return launch_mount(
            self._backend,
            src,
            outdir,
            allow_writes=allow_writes,
            transfers=transfers,
            use_links=use_links,
            vfs_cache_mode=vfs_cache_mode,
            verbose=verbose,
            cache_dir=cache_dir,
            cache_dir_delete_on_exit=cache_dir_delete_on_exit,
            log=log,
            other_args=other_args,
        )

    def mount_s3(
        self,
        url: str,
        outdir: Path,
        allow_writes=False,
        vfs_cache_mode="full",
        dir_cache_time: str | None = "1h",
        attribute_timeout: str | None = "1h",
        vfs_disk_space_total_size: str | None = "100M",
        transfers: int | None = 128,
        modtime_strategy: (ModTimeStrategy | None) = ModTimeStrategy.USE_SERVER_MODTIME,
        vfs_read_chunk_streams: int | None = 16,
        vfs_read_chunk_size: str | None = "4M",
        vfs_fast_fingerprint: bool = True,
        vfs_refresh: bool = True,
        other_args: list[str] | None = None,
    ) -> Mount:
        """Mount a remote or directory to a local path.

        Args:
            src: Remote or directory to mount
            outdir: Local path to mount to
        """
        return launch_s3_mount(
            self,
            url,
            outdir,
            allow_writes=allow_writes,
            vfs_cache_mode=vfs_cache_mode,
            dir_cache_time=dir_cache_time,
            attribute_timeout=attribute_timeout,
            vfs_disk_space_total_size=vfs_disk_space_total_size,
            transfers=transfers,
            modtime_strategy=modtime_strategy,
            vfs_read_chunk_streams=vfs_read_chunk_streams,
            vfs_read_chunk_size=vfs_read_chunk_size,
            vfs_fast_fingerprint=vfs_fast_fingerprint,
            vfs_refresh=vfs_refresh,
            other_args=other_args,
        )

    def serve_webdav(
        self,
        src: Remote | Dir | str,
        user: str,
        password: str,
        addr: str = "localhost:2049",
        allow_other: bool = False,
        other_args: list[str] | None = None,
    ) -> Process:
        """Serve a remote or directory via NFS.

        Args:
            src: Remote or directory to serve
            addr: Network address and port to serve on (default: localhost:2049)
            allow_other: Allow other users to access the share

        Returns:
            Process: The running webdev server process

        Raises:
            ValueError: If the NFS server fails to start
        """
        return launch_webdav_server(
            self._backend,
            src,
            user,
            password,
            addr=addr,
            allow_other=allow_other,
            other_args=other_args,
        )

    def serve_http(
        self,
        src: str,
        addr: str | None = None,
        other_args: list[str] | None = None,
    ) -> HttpServer:
        """Serve a remote or directory via HTTP.

        Args:
            src: Remote or directory to serve
            addr: Network address and port to serve on (default: localhost:8080)
        """
        return launch_http_server(
            self._backend,
            src,
            "minimal",
            addr=addr,
            other_args=other_args,
        )

    def config_paths(
        self, remote: str | None = None, obscure: bool = False, no_obscure: bool = False
    ) -> list[Path]:
        """Return the filesystem paths reported by `rclone config paths`:
        the config file, cache directory, and temp directory, in that fixed
        order.

        `remote`, `obscure`, and `no_obscure` are accepted for backward
        compatibility with this method's public signature. `config paths`
        takes no such arguments upstream, so they are ignored.

        Raises:
            RcloneCommandError: if the underlying `rclone config paths`
                invocation fails.
        """
        return fetch_config_paths(
            self._backend,
            remote=remote,
            obscure=obscure,
            no_obscure=no_obscure,
        )

    def config_show(
        self, remote: str | None = None, obscure: bool = False, no_obscure: bool = False
    ) -> str:
        """Return the configuration text reported by `rclone config show`.

        Raises:
            ValueError: if both `obscure` and `no_obscure` are set.
            RcloneCommandError: if the underlying `rclone config show`
                invocation fails.
        """
        return fetch_config_show(
            self._backend,
            remote=remote,
            obscure=obscure,
            no_obscure=no_obscure,
        )

    def size_files(
        self,
        src: str,
        files: list[str],
        fast_list: bool = False,
        other_args: list[str] | None = None,
        check: bool | None = False,
        verbose: bool | None = None,
    ) -> SizeResult:
        """Get the size of a list of files. Example of files items: "remote:bucket/to/file"."""
        return fetch_size_files(
            self._backend,
            self,
            src,
            files,
            fast_list=fast_list,
            other_args=other_args,
            check=check,
            verbose=verbose,
        )
