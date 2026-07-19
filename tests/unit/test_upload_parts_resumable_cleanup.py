"""Unit tests for `rclone_kit.s3.multipart.upload_parts_resumable`'s
temporary-directory exit-cleanup registry.

`upload_parts_resumable()` itself needs a real `RcloneImpl`, HTTP server,
and executors to run end-to-end, so these tests exercise the registry
(`_TMP_UPLOAD_DIRS`/`_cleanup_tmp_upload_dirs`) directly instead: the piece
that replaced a per-call `atexit.register(...)` closure (one leaked
registration per resumable upload) with a single import-time registration
draining every still-tracked directory.
"""

from pathlib import Path

import pytest

from rclone_kit.s3.multipart import upload_parts_resumable as upload_parts_resumable_module


@pytest.fixture(autouse=True)
def _isolated_cleanup_registry():
    upload_parts_resumable_module._TMP_UPLOAD_DIRS.clear()
    yield
    upload_parts_resumable_module._TMP_UPLOAD_DIRS.clear()


def test_cleanup_tmp_upload_dirs_removes_tracked_directories(tmp_path: Path) -> None:
    tracked_dir = tmp_path / "chunks-abc123"
    tracked_dir.mkdir()
    (tracked_dir / "part.bin").write_bytes(b"data")
    upload_parts_resumable_module._TMP_UPLOAD_DIRS.add(tracked_dir)

    upload_parts_resumable_module._cleanup_tmp_upload_dirs()

    assert not tracked_dir.exists()


def test_cleanup_tmp_upload_dirs_ignores_already_missing_directories(tmp_path: Path) -> None:
    missing_dir = tmp_path / "chunks-already-gone"
    upload_parts_resumable_module._TMP_UPLOAD_DIRS.add(missing_dir)

    upload_parts_resumable_module._cleanup_tmp_upload_dirs()


def test_registry_discard_stops_a_directory_from_being_cleaned_up(tmp_path: Path) -> None:
    kept_dir = tmp_path / "chunks-keep-me"
    kept_dir.mkdir()
    upload_parts_resumable_module._TMP_UPLOAD_DIRS.add(kept_dir)
    upload_parts_resumable_module._TMP_UPLOAD_DIRS.discard(kept_dir)

    upload_parts_resumable_module._cleanup_tmp_upload_dirs()

    assert kept_dir.exists()
