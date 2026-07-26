"""Unit tests for `rclone_kit.runtime.hashing`."""

import hashlib
from pathlib import Path

from rclone_kit.runtime.hashing import sha256_of_file


def test_sha256_of_file_matches_hashlib(tmp_path: Path) -> None:
    content = b"some rclone-shaped bytes"
    target = tmp_path / "payload.bin"
    target.write_bytes(content)

    assert sha256_of_file(target) == hashlib.sha256(content).hexdigest()
