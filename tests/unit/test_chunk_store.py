"""Unit tests for `rclone_kit.chunk_store`'s staging-directory policy.

The staging root is the one place that decides where the library writes
large temporary files, so these tests pin down that it is the operating
system's temporary directory rather than the current working directory,
that `RCLONE_KIT_TMP_DIR` relocates it, and that `get_chunk_tmpdir`'s
first-use memoization and stale-file pruning survived the move.
"""

import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from rclone_kit import chunk_store
from rclone_kit.chunk_store import STAGING_DIR_ENV_VAR, get_chunk_tmpdir, get_staging_root

_MEMOIZED_TMPDIR_KEY = "out"


@pytest.fixture(autouse=True)
def _isolated_chunk_tmpdir_memo() -> Iterator[None]:
    """Drop the process-wide memo so each test resolves the location itself."""
    get_chunk_tmpdir.__dict__.pop(_MEMOIZED_TMPDIR_KEY, None)
    yield
    get_chunk_tmpdir.__dict__.pop(_MEMOIZED_TMPDIR_KEY, None)


def test_default_staging_root_is_under_the_os_temp_dir_not_the_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(STAGING_DIR_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)

    root = get_staging_root()

    assert root == Path(tempfile.gettempdir()) / chunk_store._STAGING_DIR_NAME
    assert not root.is_relative_to(Path.cwd())


def test_staging_dir_env_var_overrides_the_default_location(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "fast-volume"
    monkeypatch.setenv(STAGING_DIR_ENV_VAR, str(override))

    assert get_staging_root() == override


def test_get_chunk_tmpdir_creates_the_store_under_the_os_temp_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(STAGING_DIR_ENV_VAR, raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    working_dir = tmp_path / "cwd"
    working_dir.mkdir()
    monkeypatch.chdir(working_dir)

    out = get_chunk_tmpdir()

    assert out == tmp_path / chunk_store._STAGING_DIR_NAME / chunk_store._CHUNK_STORE_DIR_NAME
    assert out.is_dir()
    assert list(working_dir.iterdir()) == []


def test_get_chunk_tmpdir_creates_the_store_under_the_configured_staging_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(STAGING_DIR_ENV_VAR, str(tmp_path))

    out = get_chunk_tmpdir()

    assert out == tmp_path / chunk_store._CHUNK_STORE_DIR_NAME
    assert out.is_dir()


def test_get_chunk_tmpdir_memoizes_the_location_it_resolved_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(STAGING_DIR_ENV_VAR, str(tmp_path / "first"))
    first = get_chunk_tmpdir()

    monkeypatch.setenv(STAGING_DIR_ENV_VAR, str(tmp_path / "second"))

    assert get_chunk_tmpdir() == first
    assert not (tmp_path / "second").exists()


def test_get_chunk_tmpdir_prunes_stale_files_and_the_directories_they_emptied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / chunk_store._CHUNK_STORE_DIR_NAME
    abandoned_dir = store / "abandoned-upload"
    abandoned_dir.mkdir(parents=True)
    stale_chunk = abandoned_dir / "stale.chunk"
    stale_chunk.write_bytes(b"stale")
    fresh_chunk = store / "fresh.chunk"
    fresh_chunk.write_bytes(b"fresh")
    stale_mtime = time.time() - (
        (chunk_store._STALE_FILE_AGE_DAYS + 1) * chunk_store._SECONDS_PER_DAY
    )
    os.utime(stale_chunk, (stale_mtime, stale_mtime))
    monkeypatch.setenv(STAGING_DIR_ENV_VAR, str(tmp_path))

    assert get_chunk_tmpdir() == store
    assert not stale_chunk.exists()
    assert not abandoned_dir.exists()
    assert fresh_chunk.exists()
