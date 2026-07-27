"""Unit tests for `rclone_kit.operations.bytes_ops`.

Drives the temp-file staging with a fake single-file transfer access, so
the S3 shortcut, the `copy_to` fallback, and both failure translations
are covered without a runtime or a real remote.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from rclone_kit.exceptions import OperationFailedError, RcloneCommandError
from rclone_kit.file import File
from rclone_kit.operation import OperationResult
from rclone_kit.operations.bytes_ops import (
    _COPY_TO_COMMAND,
    _TEMP_FILE_NAME,
    read_bytes_via_temp_file,
    write_bytes_via_temp_file,
)

_PAYLOAD = b"payload-bytes"
_S3_DST = "s3remote:bucket/object.bin"
_PLAIN_DST = "remote:dir/object.bin"
_PLAIN_SRC = "remote:dir/object.bin"
_COPY_TO_ERROR = "directory not found"
_NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


def _copy_to_result(*, ok: bool, error: str | None) -> OperationResult:
    return OperationResult(
        ok=ok,
        operation="copy_to",
        source=_PLAIN_SRC,
        destination=_PLAIN_DST,
        job_ids=(1,),
        stats=None,
        warnings=(),
        attempts=(),
        started_at=_NOW,
        ended_at=_NOW,
        duration=0.0,
        cancelled=False,
        error=error,
    )


class FakeBytesAccess:
    """A `BytesAccess` whose `copy_to` writes real local files, so a test
    can assert on the staged temp file the production code hands it.

    `produces_output=False` reproduces rclone reporting success without
    writing the destination.
    """

    def __init__(
        self,
        *,
        is_s3: bool = False,
        copy_to_error: str | None = None,
        produces_output: bool = True,
        source_payload: bytes = b"",
    ) -> None:
        self._is_s3 = is_s3
        self._copy_to_error = copy_to_error
        self._produces_output = produces_output
        self._source_payload = source_payload
        self.is_s3_queries: list[str] = []
        self.s3_uploads: list[tuple[bytes, str, bool | None]] = []
        self.copy_to_calls: list[tuple[str, str, bool | None]] = []

    def is_s3(self, dst: str) -> bool:
        self.is_s3_queries.append(dst)
        return self._is_s3

    def copy_file_s3(self, src: Path, dst: str, verbose: bool | None = None) -> None:
        self.s3_uploads.append((src.read_bytes(), dst, verbose))

    def copy_to(
        self, src: File | str, dst: File | str, check: bool | None = None
    ) -> OperationResult:
        self.copy_to_calls.append((str(src), str(dst), check))
        if self._copy_to_error is not None:
            raise OperationFailedError(_copy_to_result(ok=False, error=self._copy_to_error))
        destination = Path(str(dst))
        if self._produces_output and destination.parent.is_dir():
            destination.write_bytes(self._source_payload)
        return _copy_to_result(ok=True, error=None)


def test_write_bytes_stages_the_payload_and_copies_it_to_a_plain_remote() -> None:
    access = FakeBytesAccess()

    write_bytes_via_temp_file(access, _PAYLOAD, _PLAIN_DST)

    staged_source, destination, check = access.copy_to_calls[0]
    assert destination == _PLAIN_DST
    assert Path(staged_source).name == _TEMP_FILE_NAME
    assert check is True
    assert access.s3_uploads == []


def test_write_bytes_uploads_directly_without_copy_to_when_the_destination_is_s3() -> None:
    access = FakeBytesAccess(is_s3=True)

    write_bytes_via_temp_file(access, _PAYLOAD, _S3_DST)

    assert access.s3_uploads == [(_PAYLOAD, _S3_DST, None)]
    assert access.copy_to_calls == []


def test_write_bytes_reports_a_copy_to_failure_as_an_rclone_command_error() -> None:
    access = FakeBytesAccess(copy_to_error=_COPY_TO_ERROR)

    with pytest.raises(RcloneCommandError) as raised:
        write_bytes_via_temp_file(access, _PAYLOAD, _PLAIN_DST)

    assert raised.value.command == _COPY_TO_COMMAND
    assert raised.value.stderr == _COPY_TO_ERROR
    assert isinstance(raised.value.cause, OperationFailedError)


def test_read_bytes_returns_the_content_copy_to_produced() -> None:
    access = FakeBytesAccess(source_payload=_PAYLOAD)

    assert read_bytes_via_temp_file(access, _PLAIN_SRC) == _PAYLOAD


def test_read_bytes_reports_a_copy_to_failure_as_an_rclone_command_error() -> None:
    access = FakeBytesAccess(copy_to_error=_COPY_TO_ERROR)

    with pytest.raises(RcloneCommandError) as raised:
        read_bytes_via_temp_file(access, _PLAIN_SRC)

    assert raised.value.stderr == _COPY_TO_ERROR


def test_read_bytes_rejects_a_reported_success_that_produced_no_file() -> None:
    """rclone answering "ok" without writing the output file must not be
    read as an empty object - the caller would silently get `b""`.
    """
    access = FakeBytesAccess(produces_output=False)

    with pytest.raises(RcloneCommandError) as raised:
        read_bytes_via_temp_file(access, _PLAIN_SRC)

    assert isinstance(raised.value.cause, FileNotFoundError)
