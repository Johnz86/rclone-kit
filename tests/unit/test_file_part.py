"""Unit tests for `rclone_kit.file_part.FilePart`'s resource-ownership
lifecycle: pruning the module-level exit-cleanup registry on disposal,
idempotent (silent-on-repeat) `dispose()`, and registering the module's
`atexit` handler at most once no matter how many chunk files are staged.
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


def test_add_for_cleanup_registers_atexit_handler_at_most_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    register_calls: list[object] = []
    monkeypatch.setattr(file_part_module.atexit, "register", register_calls.append)
    monkeypatch.setattr(file_part_module._register_exit_cleanup_handlers, "__dict__", {})

    first_chunk = tmp_path / "first.chunk"
    second_chunk = tmp_path / "second.chunk"

    file_part_module._add_for_cleanup(first_chunk)
    file_part_module._add_for_cleanup(second_chunk)

    assert register_calls == [file_part_module._on_exit_cleanup]

    file_part_module._remove_from_cleanup(first_chunk)
    file_part_module._remove_from_cleanup(second_chunk)
