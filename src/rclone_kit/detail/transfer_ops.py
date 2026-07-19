import subprocess
from pathlib import Path

from rclone_kit.completed_process import CompletedProcess
from rclone_kit.convert import convert_to_str
from rclone_kit.dir import Dir
from rclone_kit.exceptions import RcloneCommandError
from rclone_kit.file import File
from rclone_kit.rclone_impl import (
    FLAG_CHECKERS,
    FLAG_LOW_LEVEL_RETRIES,
    FLAG_MULTI_THREAD_STREAMS,
    FLAG_S3_NO_CHECK_BUCKET,
    FLAG_TRANSFERS,
    RcloneImpl,
)
from rclone_kit.remote import Remote
from rclone_kit.types import SizeSuffix
from rclone_kit.util import get_check, get_verbose


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
