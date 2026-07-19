"""Shared hashing and atomic-replacement primitives for the rclone runtime
package.

Both the verified downloader and the executable resolver need to hash files
and replace cache entries atomically. Keeping these primitives in one module
avoids duplicating the same `hashlib`/`os.replace` logic in each caller.
"""

import contextlib
import hashlib
import os
from pathlib import Path

from rclone_kit.runtime.exceptions import CacheReplacementError

_HASH_CHUNK_SIZE_BYTES = 1024 * 1024


def best_effort_unlink(path: Path) -> None:
    """Delete `path` if it exists, silently ignoring any `OSError`."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def sha256_of_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 digest of the file at `path`."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_replace_file(temp_path: Path, destination: Path) -> None:
    """Atomically move `temp_path` onto `destination` with `os.replace`.

    `temp_path` and `destination` must reside on the same filesystem for the
    replacement to be atomic. Raises `CacheReplacementError`, retaining the
    original `OSError` as the cause, when the replacement fails; `temp_path`
    is removed on a best-effort basis in that case.
    """
    try:
        os.replace(temp_path, destination)
    except OSError as error:
        best_effort_unlink(temp_path)
        raise CacheReplacementError(destination) from error
