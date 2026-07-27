"""Unit tests for `rclone_kit.s3.multipart.upload_parts_resumable`'s
temporary chunk directories: where they are created and how they are
cleaned up.

`upload_parts_resumable()` itself needs a real `Rclone`, HTTP server,
and executors to run end-to-end, so these tests exercise the directory
factory (`_make_chunk_staging_dir`) and the registry
(`_TMP_UPLOAD_DIRS`/`_cleanup_tmp_upload_dirs`) directly instead: the piece
that replaced a per-call `atexit.register(...)` closure (one leaked
registration per resumable upload) with a single import-time registration
draining every still-tracked directory.
"""

from pathlib import Path

import pytest

from rclone_kit.chunk_store import STAGING_DIR_ENV_VAR
from rclone_kit.s3.multipart import upload_parts_resumable as upload_parts_resumable_module


@pytest.fixture(autouse=True)
def _isolated_cleanup_registry():
    upload_parts_resumable_module._TMP_UPLOAD_DIRS.clear()
    yield
    upload_parts_resumable_module._TMP_UPLOAD_DIRS.clear()


def test_chunk_staging_dir_is_created_under_the_staging_root_not_the_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging_root = tmp_path / "staging"
    monkeypatch.setenv(STAGING_DIR_ENV_VAR, str(staging_root))
    monkeypatch.chdir(tmp_path)

    staging_dir = upload_parts_resumable_module._make_chunk_staging_dir()

    assert staging_dir.parent == staging_root
    assert staging_dir.name.startswith(upload_parts_resumable_module._UPLOAD_CHUNKS_DIR_PREFIX)
    assert list(staging_dir.iterdir()) == []
    assert [entry.name for entry in tmp_path.iterdir()] == [staging_root.name]


def test_chunk_staging_dirs_never_collide(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(STAGING_DIR_ENV_VAR, str(tmp_path))

    first = upload_parts_resumable_module._make_chunk_staging_dir()
    second = upload_parts_resumable_module._make_chunk_staging_dir()

    assert first != second


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
