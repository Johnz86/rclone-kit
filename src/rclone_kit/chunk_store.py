"""Process-wide staging directory for streamed byte-range downloads.

Shared by `FilePart`, `HttpServer`'s chunk fetches, and the S3 multipart
resumable-upload state, all of which write downloaded ranges to disk before
merging or uploading them.
"""

import os
import time
from pathlib import Path
from threading import Lock

from rclone_kit.util import locked_print

_CHUNK_STORE_DIR = Path("chunk_store")
_STALE_FILE_AGE_DAYS = 1
_SECONDS_PER_DAY = 60 * 60 * 24

_chunk_tmpdir_lock = Lock()


def _clean_old_files(out: Path) -> None:
    """Remove files older than `_STALE_FILE_AGE_DAYS` and any directories that removal leaves empty."""
    now = time.time()

    for root, _dirs, files in os.walk(out):
        for name in files:
            f = Path(root) / name
            age_days = (now - f.stat().st_mtime) / _SECONDS_PER_DAY
            if age_days > _STALE_FILE_AGE_DAYS:
                locked_print(f"Removing old file: {f}")
                f.unlink()

    for root, dirs, _files in os.walk(out):
        for dir_name in dirs:
            d = Path(root) / dir_name
            if not list(d.iterdir()):
                locked_print(f"Removing empty directory: {d}")
                d.rmdir()


def get_chunk_tmpdir() -> Path:
    """Return the process-wide chunk staging directory, creating and pruning it on first use."""
    with _chunk_tmpdir_lock:
        dat = get_chunk_tmpdir.__dict__
        if "out" in dat:
            return dat["out"]
        if _CHUNK_STORE_DIR.exists():
            _clean_old_files(_CHUNK_STORE_DIR)
        _CHUNK_STORE_DIR.mkdir(exist_ok=True, parents=True)
        dat["out"] = _CHUNK_STORE_DIR
        return _CHUNK_STORE_DIR
