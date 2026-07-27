"""Process-wide staging directory for streamed byte-range downloads.

Shared by `FilePart`, `HttpServer`'s chunk fetches, and the S3 multipart
resumable-upload state, all of which write downloaded ranges to disk before
merging or uploading them. `get_staging_root` is also the single place that
decides where every large temporary file this library writes goes.
"""

import os
import tempfile
import time
from pathlib import Path
from threading import Lock

from rclone_kit.util import locked_print

STAGING_DIR_ENV_VAR = "RCLONE_KIT_TMP_DIR"

_STAGING_DIR_NAME = "rclone-kit"
_CHUNK_STORE_DIR_NAME = "chunk_store"
_STALE_FILE_AGE_DAYS = 1
_SECONDS_PER_DAY = 60 * 60 * 24

_chunk_tmpdir_lock = Lock()


def get_staging_root() -> Path:
    """Return the directory under which the library stages large temporary files.

    Defaults to an rclone-kit-named subdirectory of the operating system's
    temporary directory; `RCLONE_KIT_TMP_DIR` overrides it so a deployment
    can put byte-range chunks and multipart upload chunks on a specific
    volume. Never the current working directory: a library that writes
    there fails outright when the process runs with a read-only or shared
    working directory, and collides with any other process started from the
    same one.

    The environment is read on every call rather than frozen into a
    module-level constant at import time, so an operator - or a test - can
    change the location after `rclone_kit` has been imported.
    """
    override = os.getenv(STAGING_DIR_ENV_VAR)
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / _STAGING_DIR_NAME


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
    """Return the process-wide chunk staging directory, creating and pruning it on first use.

    Memoized on this function's own `__dict__` so the location is resolved -
    and the store pruned - exactly once per process even though
    `get_staging_root` itself re-reads the environment on every call.
    """
    with _chunk_tmpdir_lock:
        dat = get_chunk_tmpdir.__dict__
        if "out" in dat:
            return dat["out"]
        out = get_staging_root() / _CHUNK_STORE_DIR_NAME
        if out.exists():
            _clean_old_files(out)
        out.mkdir(exist_ok=True, parents=True)
        dat["out"] = out
        return out
