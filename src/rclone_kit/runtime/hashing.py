"""Shared hashing primitive for the rclone runtime package."""

import hashlib
from pathlib import Path

_HASH_CHUNK_SIZE_BYTES = 1024 * 1024


def sha256_of_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 digest of the file at `path`."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()
