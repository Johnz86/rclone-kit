"""Embedded RC-backed transfer operations (CLI-to-C-ABI migration ledger
rows T01, T02, T07; T11/T12 (`read_bytes`/`read_text`) become transitive
once `copy_to` (T02) is embedded-aware, since `Rclone.read_bytes` only ever
calls `self.copy_to`.

`CompletedProcess` wraps one synthetic `subprocess.CompletedProcess` per
call here - there is no real subprocess, no `args` a caller could re-run,
and no captured `stdout`/`stderr` beyond a short diagnostic string. This
matches the migration plan's documented compatibility-period behavior
("preserving `ok` and a synthetic `returncode`") until `OperationResult`
replaces `CompletedProcess` for embedded callers; it is not meant to be
byte-compatible with a real CLI invocation.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from rclone_kit.completed_process import CompletedProcess
from rclone_kit.convert import convert_to_str
from rclone_kit.exceptions import UnsupportedEmbeddedOperationError
from rclone_kit.rc.client import RcCallable
from rclone_kit.rc.errors import RcCallError
from rclone_kit.rc.paths import RcPath
from rclone_kit.util import get_check

if TYPE_CHECKING:
    from rclone_kit.dir import Dir
    from rclone_kit.file import File


def _synthetic_completed_process(
    command_desc: str, *, returncode: int, stderr: str = ""
) -> CompletedProcess:
    return CompletedProcess.from_subprocess(
        subprocess.CompletedProcess(
            args=[command_desc], returncode=returncode, stdout="", stderr=stderr
        )
    )


def copy_file_to_embedded(
    rc_client: RcCallable,
    src: File | str,
    dst: File | str,
    check: bool | None = None,
    other_args: list[str] | None = None,
) -> CompletedProcess:
    """Copy one file from source to destination via `operations/copyfile`.

    Both sides split at their final path component: `operations/copyfile`
    needs `srcFs`/`dstFs` to be navigable directory roots, never the bare
    file paths themselves.

    Raises `RcCallError` when `check` resolves `True` (the default) and the
    RC call fails; otherwise returns a `CompletedProcess` whose `.ok`
    reflects success, matching the CLI backend's own `check` semantics.
    """
    if other_args:
        raise UnsupportedEmbeddedOperationError("copy_to (other_args)")
    check = get_check(check)
    src_str = src if isinstance(src, str) else str(src.path)
    dst_str = dst if isinstance(dst, str) else str(dst.path)
    src_target = RcPath.parse(src_str).as_parent_and_name()
    dst_target = RcPath.parse(dst_str).as_parent_and_name()
    command_desc = f"operations/copyfile {src_str} -> {dst_str}"
    try:
        rc_client.call(
            "operations/copyfile",
            srcFs=src_target.fs,
            srcRemote=src_target.remote,
            dstFs=dst_target.fs,
            dstRemote=dst_target.remote,
        )
    except RcCallError as error:
        if check:
            raise
        return _synthetic_completed_process(command_desc, returncode=1, stderr=str(error))
    return _synthetic_completed_process(command_desc, returncode=0)


def purge_dir_embedded(rc_client: RcCallable, src: str | Dir) -> CompletedProcess:
    """Purge a directory and all of its contents via `operations/purge`.

    Never raises: matches the CLI backend's own `purge_dir`, which always
    returns a `CompletedProcess` for the caller to inspect via `.ok`.
    """
    src_str = convert_to_str(src)
    target = RcPath.parse(src_str)
    command_desc = f"operations/purge {src_str}"
    try:
        rc_client.call("operations/purge", fs=target.fs, remote=target.remote)
    except RcCallError as error:
        return _synthetic_completed_process(command_desc, returncode=1, stderr=str(error))
    return _synthetic_completed_process(command_desc, returncode=0)


def cleanup_embedded(rc_client: RcCallable, src: str) -> CompletedProcess:
    """Remove trashed files in `src` via `operations/cleanup`.

    Never raises, matching the CLI backend's own `cleanup`.
    """
    command_desc = f"operations/cleanup {src}"
    try:
        rc_client.call("operations/cleanup", fs=src)
    except RcCallError as error:
        return _synthetic_completed_process(command_desc, returncode=1, stderr=str(error))
    return _synthetic_completed_process(command_desc, returncode=0)
