"""Unit tests for `rclone_kit.file_part.FilePart`'s resource-ownership
lifecycle: pruning the module-level exit-cleanup registry on disposal, and
idempotent (silent-on-repeat) `dispose()`.
"""

from pathlib import Path

import pytest

from rclone_kit import file_part as file_part_module
from rclone_kit.file_part import FilePart
from rclone_kit.s3.multipart.file_info import S3FileInfo


def _s3_file_info() -> S3FileInfo:
    return S3FileInfo(upload_id="upload-id", part_number=1)


def test_dispose_deletes_file_and_prunes_cleanup_registry(tmp_path: Path) -> None:
    chunk = tmp_path / "chunk.bin"
    chunk.write_bytes(b"data")
    part = FilePart(payload=chunk, extra=_s3_file_info())

    assert chunk in file_part_module._CLEANUP_LIST

    part.dispose()

    assert not chunk.exists()
    assert chunk not in file_part_module._CLEANUP_LIST


def test_dispose_is_idempotent_and_silent_on_repeat(
    tmp_path: Path, recwarn: pytest.WarningsRecorder
) -> None:
    chunk = tmp_path / "chunk.bin"
    chunk.write_bytes(b"data")
    part = FilePart(payload=chunk, extra=_s3_file_info())

    part.dispose()
    recwarn.clear()

    part.dispose()

    assert len(recwarn) == 0


def test_dispose_on_error_payload_warns_once_then_is_silent(
    recwarn: pytest.WarningsRecorder,
) -> None:
    part = FilePart(payload=OSError("fetch failed"), extra=_s3_file_info())

    part.dispose()
    assert len(recwarn) == 1

    recwarn.clear()
    part.dispose()

    assert len(recwarn) == 0
