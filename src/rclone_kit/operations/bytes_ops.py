"""Whole-object reads and writes staged through a local temp file.

rclone moves objects between filesystems, never between a filesystem and
a Python buffer, so `Rclone.read_bytes()`/`write_bytes()` have to
materialize the payload as a real file and run an ordinary single-file
copy over it. That staging - and the translation of the resulting
`OperationFailedError` into the `RcloneCommandError("copyto", ...)` these
two entry points have always reported - is all this module does.

`write_bytes` prefers the direct boto3 upload when the destination is an
S3 remote (one `PutObject` instead of a staged rclone transfer), which is
why it goes through the caller's own `is_s3()`/`copy_file_s3()` rather
than calling `copy_to()` unconditionally.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Protocol

from rclone_kit.exceptions import OperationFailedError, RcloneCommandError

if TYPE_CHECKING:
    from rclone_kit.file import File
    from rclone_kit.operation import OperationResult

_COPY_TO_COMMAND = "copyto"
_TEMP_FILE_NAME = "file.bin"


class BytesAccess(Protocol):
    """The single-file transfer callbacks byte staging is built on."""

    def is_s3(self, dst: str) -> bool: ...

    def copy_file_s3(self, src: Path, dst: str, verbose: bool | None = None) -> None: ...

    def copy_to(
        self, src: File | str, dst: File | str, check: bool | None = None
    ) -> OperationResult: ...


def _copy_to_failure_detail(error: OperationFailedError) -> str:
    """Extract a stderr-like diagnostic string from a `copy_to()` failure."""
    return error.result.error or ""


def _copy_to_checked(access: BytesAccess, src: str, dst: str) -> None:
    """Run `access.copy_to(check=True)`, reporting a failure as the
    `RcloneCommandError` both byte entry points document.
    """
    try:
        access.copy_to(src, dst, check=True)
    except OperationFailedError as error:
        raise RcloneCommandError(_COPY_TO_COMMAND, _copy_to_failure_detail(error), error) from error


def write_bytes_via_temp_file(access: BytesAccess, data: bytes, dst: str) -> None:
    """Write `data` to `dst`, uploading directly when `dst` is S3.

    Raises `RcloneCommandError` if the underlying operation fails.
    """
    with TemporaryDirectory() as tmpdir:
        tmpfile = Path(tmpdir) / _TEMP_FILE_NAME
        tmpfile.write_bytes(data)
        if access.is_s3(dst):
            access.copy_file_s3(tmpfile, dst)
            return
        _copy_to_checked(access, str(tmpfile), dst)


def read_bytes_via_temp_file(access: BytesAccess, src: str) -> bytes:
    """Read the whole object at `src` into memory.

    Raises `RcloneCommandError` if the underlying operation fails or if
    rclone reports success without producing an output file.
    """
    with TemporaryDirectory() as tmpdir:
        tmpfile = Path(tmpdir) / _TEMP_FILE_NAME
        _copy_to_checked(access, src, str(tmpfile))
        if not tmpfile.exists():
            raise RcloneCommandError(
                _COPY_TO_COMMAND, "", FileNotFoundError(f"{src} produced no output file")
            )
        return tmpfile.read_bytes()
