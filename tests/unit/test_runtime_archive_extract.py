"""Unit tests for `rclone_kit.runtime.archive_extract`."""

import zipfile
from pathlib import Path

import pytest

from rclone_kit.runtime.archive_extract import extract_single_member
from rclone_kit.runtime.exceptions import (
    ArchiveMemberDuplicateError,
    ArchiveMemberMissingError,
    ArchiveMemberUnsafeError,
)

_MEMBER_NAME = "rclone-test/rclone"
_MEMBER_CONTENT = b"fake-rclone-binary-bytes"

_UNSAFE_MEMBER_NAMES = ["/etc/passwd", "../../etc/passwd", "rclone-test/../../evil"]
_UNSAFE_MEMBER_NAME_IDS = ["absolute_path", "leading_traversal", "embedded_traversal"]


def _write_zip(path: Path, entries: list[tuple[str, bytes]]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries:
            info = zipfile.ZipInfo(name)
            archive.writestr(info, content)
    return path


def test_extract_single_member_writes_expected_bytes(tmp_path: Path) -> None:
    archive_path = _write_zip(tmp_path / "archive.zip", [(_MEMBER_NAME, _MEMBER_CONTENT)])
    destination = tmp_path / "out" / "rclone"

    result = extract_single_member(archive_path, _MEMBER_NAME, destination)

    assert result == destination
    assert destination.read_bytes() == _MEMBER_CONTENT


def test_extract_single_member_raises_when_missing(tmp_path: Path) -> None:
    archive_path = _write_zip(tmp_path / "archive.zip", [("other/file", b"x")])

    with pytest.raises(ArchiveMemberMissingError):
        extract_single_member(archive_path, _MEMBER_NAME, tmp_path / "out" / "rclone")


def test_extract_single_member_raises_on_duplicate_entries(tmp_path: Path) -> None:
    archive_path = _write_zip(
        tmp_path / "archive.zip",
        [(_MEMBER_NAME, _MEMBER_CONTENT), (_MEMBER_NAME, b"other-content")],
    )

    with pytest.raises(ArchiveMemberDuplicateError):
        extract_single_member(archive_path, _MEMBER_NAME, tmp_path / "out" / "rclone")


@pytest.mark.parametrize("unsafe_member_name", _UNSAFE_MEMBER_NAMES, ids=_UNSAFE_MEMBER_NAME_IDS)
def test_extract_single_member_rejects_unsafe_paths(
    tmp_path: Path, unsafe_member_name: str
) -> None:
    archive_path = _write_zip(tmp_path / "archive.zip", [(unsafe_member_name, _MEMBER_CONTENT)])

    with pytest.raises(ArchiveMemberUnsafeError):
        extract_single_member(archive_path, unsafe_member_name, tmp_path / "out" / "rclone")
