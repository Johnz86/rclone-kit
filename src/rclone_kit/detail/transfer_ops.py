import logging
import subprocess
import warnings
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from rclone_kit.completed_process import CompletedProcess
from rclone_kit.convert import convert_to_str
from rclone_kit.dir import Dir
from rclone_kit.exceptions import RcloneCommandError
from rclone_kit.file import File
from rclone_kit.group_files import group_files
from rclone_kit.rclone_impl import (
    FLAG_CHECKERS,
    FLAG_FAST_LIST,
    FLAG_FILES_FROM,
    FLAG_LOW_LEVEL_RETRIES,
    FLAG_MULTI_THREAD_STREAMS,
    FLAG_PROGRESS,
    FLAG_S3_NO_CHECK_BUCKET,
    FLAG_TRANSFERS,
    RcloneImpl,
)
from rclone_kit.remote import Remote
from rclone_kit.types import SizeSuffix
from rclone_kit.util import get_check, get_verbose

logger = logging.getLogger(__name__)


def copy_file_to(
    self: RcloneImpl,
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


def copy_tree(
    self: RcloneImpl,
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
    """Copy files from source to destination."""
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


def purge_dir(self: RcloneImpl, src: Dir | str) -> CompletedProcess:
    """Purge a directory"""
    src = src if isinstance(src, str) else str(src.path)
    cmd_list: list[str] = ["purge", str(src)]
    cp = self._run(cmd_list)
    return CompletedProcess.from_subprocess(cp)


def copy_byte_range(
    self: RcloneImpl,
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


def copy_directory(
    self: RcloneImpl, src: str | Dir, dst: str | Dir, args: list[str] | None = None
) -> CompletedProcess:
    """Copy a directory from source to destination."""
    src = convert_to_str(src)
    dst = convert_to_str(dst)
    cmd_list: list[str] = ["copy", src, dst, FLAG_S3_NO_CHECK_BUCKET]
    if args is not None:
        cmd_list += args
    cp = self._run(cmd_list)
    return CompletedProcess.from_subprocess(cp)


def copy_between_remotes(
    self: RcloneImpl, src: Remote, dst: Remote, args: list[str] | None = None
) -> CompletedProcess:
    """Copy a remote to another remote."""
    cmd_list: list[str] = ["copy", str(src), str(dst), FLAG_S3_NO_CHECK_BUCKET]
    if args is not None:
        cmd_list += args

    cp = self._run(cmd_list)
    return CompletedProcess.from_subprocess(cp)


def copy_files_partitioned(
    self: RcloneImpl,
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
                        filelist = [f.strip() for f in files.read_text().splitlines() if f.strip()]
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
