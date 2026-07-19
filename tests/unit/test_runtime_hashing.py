"""Unit tests for `rclone_kit.runtime.hashing`."""

import hashlib
from pathlib import Path

import pytest

from rclone_kit.runtime import hashing
from rclone_kit.runtime.exceptions import CacheReplacementError
from rclone_kit.runtime.hashing import atomic_replace_file, sha256_of_file


def test_sha256_of_file_matches_hashlib(tmp_path: Path) -> None:
    content = b"some rclone-shaped bytes"
    target = tmp_path / "payload.bin"
    target.write_bytes(content)

    assert sha256_of_file(target) == hashlib.sha256(content).hexdigest()


def test_atomic_replace_file_moves_temp_onto_destination(tmp_path: Path) -> None:
    temp_path = tmp_path / "candidate.tmp"
    temp_path.write_bytes(b"payload")
    destination = tmp_path / "final"

    atomic_replace_file(temp_path, destination)

    assert destination.read_bytes() == b"payload"
    assert not temp_path.exists()


def test_atomic_replace_file_overwrites_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "final"
    destination.write_bytes(b"stale")
    temp_path = tmp_path / "candidate.tmp"
    temp_path.write_bytes(b"fresh")

    atomic_replace_file(temp_path, destination)

    assert destination.read_bytes() == b"fresh"


def test_atomic_replace_file_raises_cache_replacement_error_on_os_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp_path = tmp_path / "candidate.tmp"
    temp_path.write_bytes(b"payload")
    destination = tmp_path / "final"

    def failing_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(hashing.os, "replace", failing_replace)

    with pytest.raises(CacheReplacementError) as excinfo:
        atomic_replace_file(temp_path, destination)

    assert excinfo.value.destination == destination
    assert isinstance(excinfo.value.__cause__, OSError)
    assert not destination.exists()
