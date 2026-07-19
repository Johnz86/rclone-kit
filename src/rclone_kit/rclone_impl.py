"""Rclone implementation providing the public operation surface."""

from __future__ import annotations

import logging
import os
import subprocess
import time
import warnings
from collections.abc import Generator
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from rclone_kit import Dir
from rclone_kit.completed_process import CompletedProcess
from rclone_kit.config import Config
from rclone_kit.convert import convert_to_filestr_list, convert_to_str
from rclone_kit.detail.walk import walk
from rclone_kit.diff import DiffItem, DiffOption
from rclone_kit.dir_listing import DirListing
from rclone_kit.exceptions import RcloneCommandError
from rclone_kit.exec import RcloneExec
from rclone_kit.file import File
from rclone_kit.file_stream import FilesStream
from rclone_kit.fs.filesystem import FSPath, RemoteFS
from rclone_kit.group_files import group_files
from rclone_kit.http_server import HttpServer
from rclone_kit.mount import Mount
from rclone_kit.optional_dependency import MissingOptionalDependencyError
from rclone_kit.process import Process
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath
from rclone_kit.types import (
    ListingOption,
    ModTimeStrategy,
    Order,
    PartInfo,
    SizeResult,
    SizeSuffix,
)
from rclone_kit.util import (
    get_check,
    get_rclone_exe,
    get_verbose,
    to_path,
)

if TYPE_CHECKING:
    from rclone_kit.s3.api import S3Client
    from rclone_kit.s3.types import S3Credentials

logger = logging.getLogger(__name__)

FLAG_CHECKERS = "--checkers"
FLAG_FAST_LIST = "--fast-list"
FLAG_FILES_FROM = "--files-from"
FLAG_LOW_LEVEL_RETRIES = "--low-level-retries"
FLAG_MULTI_THREAD_STREAMS = "--multi-thread-streams"
FLAG_PROGRESS = "--progress"
FLAG_S3_NO_CHECK_BUCKET = "--s3-no-check-bucket"
FLAG_TRANSFERS = "--transfers"
FLAG_VFS_CACHE_MODE = "--vfs-cache-mode"


def rclone_verbose(verbose: bool | None) -> bool:
    if verbose is not None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1" if verbose else "0"
    return bool(int(os.getenv("RCLONE_KIT_VERBOSE", "0")))


def _to_rclone_conf(config: Config | Path | None) -> Config:
    if config is None:
        return Config(None)
    elif isinstance(config, Path):
        content = config.read_text(encoding="utf-8")
        return Config(content)
    else:
        return config


class RcloneImpl:
    def __init__(self, rclone_conf: Path | Config | None, rclone_exe: Path | None = None) -> None:
        if isinstance(rclone_conf, Path) and not rclone_conf.exists():
            raise ValueError(f"Rclone config file not found: {rclone_conf}")
        rclone_exe = get_rclone_exe(rclone_exe)

        self._exec = RcloneExec(None, get_rclone_exe(rclone_exe))
        if rclone_conf is None:
            from rclone_kit.config import find_conf_file

            maybe_path = find_conf_file(self)
            if not isinstance(maybe_path, Path):
                warnings.warn("Rclone config file not found", stacklevel=2)
            rclone_conf = _to_rclone_conf(maybe_path)

        self._exec = RcloneExec(rclone_conf, get_rclone_exe(rclone_exe))
        self.config: Config = _to_rclone_conf(rclone_conf)

    def _run(
        self, cmd: list[str], check: bool = False, capture: bool | Path | None = None
    ) -> subprocess.CompletedProcess:
        return self._exec.execute(cmd, check=check, capture=capture)

    def _launch_process(
        self, cmd: list[str], capture: bool | None = None, log: Path | None = None
    ) -> Process:
        return self._exec.launch_process(cmd, capture=capture, log=log)

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
        return RemoteFS(self.config, src)

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
        from rclone_kit.detail.config_ops import obscure_password

        return obscure_password(self, password)

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
        from rclone_kit.detail.listing_ops import fetch_ls

        return fetch_ls(
            self,
            src,
            max_depth=max_depth,
            glob=glob,
            order=order,
            listing_option=listing_option,
        )

    def print(self, src: str) -> None:
        """Print the contents of a file."""
        from rclone_kit.detail.listing_ops import print_contents

        print_contents(self, src)

    def stat(self, src: str) -> File:
        """Get the status of a file or directory.

        Raises FileNotFoundError if `src` does not exist.
        """
        from rclone_kit.detail.listing_ops import fetch_stat

        return fetch_stat(self, src)

    def modtime(self, src: str) -> str:
        """Get the modification time of a file or directory."""
        from rclone_kit.detail.listing_ops import fetch_modtime

        return fetch_modtime(self, src)

    def modtime_dt(self, src: str) -> datetime:
        """Get the modification time of a file or directory."""
        from rclone_kit.detail.listing_ops import fetch_modtime_dt

        return fetch_modtime_dt(self, src)

    def listremotes(self) -> list[Remote]:
        from rclone_kit.detail.listing_ops import fetch_listremotes

        return fetch_listremotes(self)

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
        from rclone_kit.detail.listing_ops import stream_diff

        yield from stream_diff(
            self,
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
        """Walk through the given path recursively.

        WORK IN PROGRESS!!

        Args:
            src: Source directory or Remote to walk through
            dst: Destination directory or Remote to walk through
            max_depth: Maximum depth to traverse (-1 for unlimited)

        Yields:
            DirListing: Directory listing for each directory encountered
        """
        from rclone_kit.scan_missing_folders import scan_missing_folders

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
        check = get_check(check)
        verbose = get_verbose(verbose)
        src = src if isinstance(src, str) else str(src.path)
        dst = dst if isinstance(dst, str) else str(dst.path)
        cmd_list: list[str] = [
            "copyto",
            src,
            dst,
            FLAG_S3_NO_CHECK_BUCKET,
            "--no-traverse",
        ]
        if other_args is not None:
            cmd_list += other_args
        cp = self._run(cmd_list, check=check)
        return CompletedProcess.from_subprocess(cp)

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
        check = get_check(check)
        max_partition_workers = 1 if max_partition_workers is None else max_partition_workers
        low_level_retries = 10 if low_level_retries is None else low_level_retries
        retries = 3 if retries is None else retries
        command_args = [*(other_args or ()), FLAG_S3_NO_CHECK_BUCKET]
        checkers = 1000 if checkers is None else checkers
        transfers = 32 if transfers is None else transfers
        verbose = get_verbose(verbose)
        payload: list[str] = (
            files
            if isinstance(files, list)
            else [f.strip() for f in files.read_text().splitlines() if f.strip()]
        )
        if len(payload) == 0:
            return []

        for p in payload:
            if ":" in p:
                raise ValueError(
                    f"Invalid file path, contains a remote, which is not allowed for copy_files: {p}"
                )

        using_fast_list = FLAG_FAST_LIST in command_args
        if using_fast_list:
            warnings.warn(
                "It's not recommended to use --fast-list with copy_files as this will perform poorly on large repositories since the entire repository has to be scanned.",
                stacklevel=2,
            )

        if max_partition_workers > 1:
            datalists: dict[str, list[str]] = group_files(payload, fully_qualified=False)
        else:
            datalists = {"": payload}

        out: list[CompletedProcess] = []

        futures: list[Future] = []

        with ThreadPoolExecutor(max_workers=max_partition_workers) as executor:
            for common_prefix, partition_files in datalists.items():

                def _task(
                    files: list[str] | Path = partition_files,
                    common_prefix: str = common_prefix,
                ) -> subprocess.CompletedProcess:
                    with TemporaryDirectory() as tmpdir:
                        filelist: list[str] = []
                        filepath: Path
                        if isinstance(files, list):
                            include_files_txt = Path(tmpdir) / "include_files.txt"
                            include_files_txt.write_text("\n".join(files), encoding="utf-8")
                            filelist = list(files)
                            filepath = Path(include_files_txt)
                        elif isinstance(files, Path):
                            filelist = [
                                f.strip() for f in files.read_text().splitlines() if f.strip()
                            ]
                            filepath = files
                        if common_prefix:
                            src_path = f"{src}/{common_prefix}"
                            dst_path = f"{dst}/{common_prefix}"
                        else:
                            src_path = src
                            dst_path = dst

                        if verbose:
                            nfiles = len(filelist)
                            files_fqdn = [f"  {src_path}/{f}" for f in filelist]
                            logger.info("Copying %d files:", nfiles)
                            chunk_size = 100
                            for i in range(0, nfiles, chunk_size):
                                chunk = files_fqdn[i : i + chunk_size]
                                files_str = "\n".join(chunk)
                                logger.info("%s", files_str)
                        cmd_list: list[str] = [
                            "copy",
                            src_path,
                            dst_path,
                            FLAG_FILES_FROM,
                            str(filepath),
                            FLAG_CHECKERS,
                            str(checkers),
                            FLAG_TRANSFERS,
                            str(transfers),
                            FLAG_LOW_LEVEL_RETRIES,
                            str(low_level_retries),
                            "--retries",
                            str(retries),
                        ]
                        if metadata:
                            cmd_list.append("--metadata")
                        if retries_sleep is not None:
                            cmd_list += ["--retries-sleep", retries_sleep]
                        if timeout is not None:
                            cmd_list += ["--timeout", timeout]
                        if max_backlog is not None:
                            cmd_list += ["--max-backlog", str(max_backlog)]
                        if multi_thread_streams is not None:
                            cmd_list += [
                                FLAG_MULTI_THREAD_STREAMS,
                                str(multi_thread_streams),
                            ]
                        if verbose:
                            if not any("-v" in x for x in command_args):
                                cmd_list.append("-vvvv")
                            if not any(FLAG_PROGRESS in x for x in command_args):
                                cmd_list.append(FLAG_PROGRESS)
                        cmd_list += command_args
                        out = self._run(cmd_list, capture=not verbose)
                        return out

                fut: Future = executor.submit(_task)
                futures.append(fut)
            for fut in futures:
                cp: subprocess.CompletedProcess = fut.result()
                assert cp is not None
                out.append(CompletedProcess.from_subprocess(cp))
                if cp.returncode != 0:
                    if check:
                        raise ValueError(f"Error deleting files: {cp.stderr}")
                    else:
                        warnings.warn(f"Error deleting files: {cp.stderr}", stacklevel=2)
        return out

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

        src_dir = convert_to_str(src)
        dst_dir = convert_to_str(dst)
        check = get_check(check)
        checkers = checkers or 1000
        transfers = transfers or 32
        low_level_retries = low_level_retries or 10
        retries = retries or 3
        cmd_list: list[str] = ["copy", src_dir, dst_dir]
        cmd_list += [FLAG_CHECKERS, str(checkers)]
        cmd_list += [FLAG_TRANSFERS, str(transfers)]
        cmd_list += [FLAG_LOW_LEVEL_RETRIES, str(low_level_retries)]
        cmd_list.append(FLAG_S3_NO_CHECK_BUCKET)
        if multi_thread_streams is not None:
            cmd_list += [FLAG_MULTI_THREAD_STREAMS, str(multi_thread_streams)]
        if other_args:
            cmd_list += other_args
        cp = self._run(cmd_list, check=check, capture=False)
        return CompletedProcess.from_subprocess(cp)

    def purge(self, src: Dir | str) -> CompletedProcess:
        """Purge a directory"""

        src = src if isinstance(src, str) else str(src.path)
        cmd_list: list[str] = ["purge", str(src)]
        cp = self._run(cmd_list)
        return CompletedProcess.from_subprocess(cp)

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
        check = get_check(check)
        verbose = get_verbose(verbose)
        payload: list[str] = convert_to_filestr_list(files)
        if len(payload) == 0:
            if verbose:
                logger.info("No files to delete")
            cp = subprocess.CompletedProcess(
                args=["rclone", "delete", FLAG_FILES_FROM, "[]"],
                returncode=0,
                stdout="",
                stderr="",
            )
            return CompletedProcess.from_subprocess(cp)

        datalists: dict[str, list[str]] = group_files(payload)
        completed_processes: list[subprocess.CompletedProcess] = []

        futures: list[Future] = []

        with ThreadPoolExecutor(max_workers=max_partition_workers) as executor:
            for remote, remote_files in datalists.items():

                def _task(
                    files=remote_files, check=check, remote=remote
                ) -> subprocess.CompletedProcess:
                    with TemporaryDirectory() as tmpdir:
                        include_files_txt = Path(tmpdir) / "include_files.txt"
                        include_files_txt.write_text("\n".join(files), encoding="utf-8")

                        cmd_list: list[str] = [
                            "delete",
                            remote,
                            FLAG_FILES_FROM,
                            str(include_files_txt),
                            FLAG_CHECKERS,
                            "1000",
                            FLAG_TRANSFERS,
                            "1000",
                        ]
                        if verbose:
                            cmd_list.append("-vvvv")
                        if rmdirs:
                            cmd_list.append("--rmdirs")
                        if other_args:
                            cmd_list += other_args
                        out = self._run(cmd_list, check=check)
                    if out.returncode != 0:
                        if check:
                            completed_processes.append(out)
                            raise ValueError(f"Error deleting files: {out}")
                        else:
                            warnings.warn(f"Error deleting files: {out}", stacklevel=2)
                    return out

                fut: Future = executor.submit(_task)
                futures.append(fut)

            for fut in futures:
                out = fut.result()
                assert out is not None
                completed_processes.append(out)

        return CompletedProcess(completed_processes)

    def exists(self, src: Dir | Remote | str | File) -> bool:
        """Check if a file or directory exists."""
        from rclone_kit.detail.listing_ops import check_exists

        return check_exists(self, src)

    def is_synced(self, src: str | Dir, dst: str | Dir) -> bool:
        """Check if two directories are in sync."""
        from rclone_kit.detail.listing_ops import check_is_synced

        return check_is_synced(self, src, dst)

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
        from rclone_kit.s3.types import S3UploadTarget
        from rclone_kit.util import S3PathInfo

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
        from rclone_kit.detail.config_ops import check_is_s3

        return check_is_s3(self, dst)

    def copy_file_s3_resumable(
        self,
        src: str,
        dst: str,
        part_infos: list[PartInfo] | None = None,
        upload_threads: int = 8,
        merge_threads: int = 4,
    ) -> None:
        """Copy parts of a file from source to destination."""
        from rclone_kit.detail.copy_file_parts_resumable import (
            copy_file_parts_resumable,
        )

        if dst.endswith("/"):
            dst = dst[:-1]
        dst_dir = f"{dst}-parts"

        copy_file_parts_resumable(
            self=self,
            src=src,
            dst_dir=dst_dir,
            part_infos=part_infos,
            upload_threads=upload_threads,
            merge_threads=merge_threads,
        )

    def write_text(
        self,
        dst: str,
        text: str,
    ) -> None:
        """Write text to a file."""
        self.write_bytes(dst=dst, data=text.encode("utf-8"))

    def write_bytes(
        self,
        dst: str,
        data: bytes | Path,
        verbose: bool | None = None,
    ) -> None:
        """Write bytes to a file.

        Raises RcloneCommandError if the underlying rclone command fails.
        """
        if isinstance(data, Path):
            data = data.read_bytes()

        with TemporaryDirectory() as tmpdir:
            tmpfile = Path(tmpdir) / "file.bin"
            tmpfile.write_bytes(data)
            if self.is_s3(dst):
                self.copy_file_s3(tmpfile, dst, verbose=verbose)
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
        from rclone_kit.detail.listing_ops import fetch_size_file

        return fetch_size_file(self, src)

    def get_s3_credentials(self, remote: str, verbose: bool | None = None) -> S3Credentials:
        from rclone_kit.detail.config_ops import fetch_s3_credentials

        return fetch_s3_credentials(self, remote, verbose=verbose)

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
        offset = SizeSuffix(offset).as_int()
        length = SizeSuffix(length).as_int()
        cmd_list: list[str] = [
            "cat",
            "--offset",
            str(offset),
            "--count",
            str(length),
            src,
        ]
        if other_args:
            cmd_list.extend(other_args)
        try:
            self._run(cmd_list, check=True, capture=outfile)
        except subprocess.CalledProcessError as error:
            raise RcloneCommandError("cat", error.stderr or "", error) from error

    def copy_dir(
        self, src: str | Dir, dst: str | Dir, args: list[str] | None = None
    ) -> CompletedProcess:
        """Copy a directory from source to destination."""

        src = convert_to_str(src)
        dst = convert_to_str(dst)
        cmd_list: list[str] = ["copy", src, dst, FLAG_S3_NO_CHECK_BUCKET]
        if args is not None:
            cmd_list += args
        cp = self._run(cmd_list)
        return CompletedProcess.from_subprocess(cp)

    def copy_remote(
        self, src: Remote, dst: Remote, args: list[str] | None = None
    ) -> CompletedProcess:
        """Copy a remote to another remote."""
        cmd_list: list[str] = ["copy", str(src), str(dst), FLAG_S3_NO_CHECK_BUCKET]
        if args is not None:
            cmd_list += args

        cp = self._run(cmd_list)
        return CompletedProcess.from_subprocess(cp)

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
        from rclone_kit.detail.mount_ops import launch_mount

        return launch_mount(
            self,
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
        from rclone_kit.detail.mount_ops import launch_s3_mount

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
        from rclone_kit.detail.serve_ops import launch_webdav_server

        return launch_webdav_server(
            self,
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
        cache_mode: str | None,
        addr: str | None = None,
        serve_http_log: Path | None = None,
        other_args: list[str] | None = None,
    ) -> HttpServer:
        """Serve a remote or directory via HTTP.

        Args:
            src: Remote or directory to serve
            addr: Network address and port to serve on (default: localhost:8080)
        """
        from rclone_kit.detail.serve_ops import launch_http_server

        return launch_http_server(
            self,
            src,
            cache_mode,
            addr=addr,
            serve_http_log=serve_http_log,
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
        from rclone_kit.detail.config_ops import fetch_config_paths

        return fetch_config_paths(self, remote=remote, obscure=obscure, no_obscure=no_obscure)

    def config_show(
        self, remote: str | None = None, obscure: bool = False, no_obscure: bool = False
    ) -> str:
        """Return the configuration text reported by `rclone config show`.

        Raises:
            ValueError: if both `obscure` and `no_obscure` are set.
            RcloneCommandError: if the underlying `rclone config show`
                invocation fails.
        """
        from rclone_kit.detail.config_ops import fetch_config_show

        return fetch_config_show(self, remote=remote, obscure=obscure, no_obscure=no_obscure)

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
        from rclone_kit.detail.listing_ops import fetch_size_files

        return fetch_size_files(
            self,
            src,
            files,
            fast_list=fast_list,
            other_args=other_args,
            check=check,
            verbose=verbose,
        )
