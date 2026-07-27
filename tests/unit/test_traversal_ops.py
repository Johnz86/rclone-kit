"""Unit tests for `rclone_kit.operations.traversal_ops`.

Covers the `Dir | Remote | str` coercion the two recursive traversals
share, including its laziness: neither entry point may touch `src` before
iteration starts.
"""

from collections.abc import Iterator
from typing import cast

import pytest

from rclone_kit.access import ListingAccess
from rclone_kit.client import Rclone
from rclone_kit.dir import Dir
from rclone_kit.operations.traversal_ops import (
    _DIRECTORY_MIME_TYPE,
    _to_walk_dir,
    scan_missing_folders_from,
    walk_from,
)
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath

_REMOTE_NAME = "remote"
_DIR_PATH = "bucket/folder"
_DIR_NAME = "folder"
_SRC = "remote:bucket/src"
_DST = "remote:bucket/dst"


@pytest.fixture
def access() -> ListingAccess:
    """An `Rclone` shell with no runtime: the coercion only ever binds the
    client onto the resulting paths, it never calls back into it.
    """
    return cast(ListingAccess, object.__new__(Rclone))


def test_to_walk_dir_accepts_a_remote_path_string(access: ListingAccess) -> None:
    walk_dir = _to_walk_dir(access, f"{_REMOTE_NAME}:{_DIR_PATH}")

    assert walk_dir.path.remote.name == _REMOTE_NAME
    assert str(walk_dir.path) == f"{_REMOTE_NAME}:{_DIR_PATH}"
    assert walk_dir.path.rclone is access


def test_to_walk_dir_accepts_a_remote(access: ListingAccess) -> None:
    walk_dir = _to_walk_dir(access, Remote(_REMOTE_NAME, access))

    assert walk_dir.path.remote.name == _REMOTE_NAME
    assert walk_dir.path.is_dir is True


def test_to_walk_dir_rebuilds_a_dir_as_a_plain_directory_bound_to_this_client(
    access: ListingAccess,
) -> None:
    """A `Dir` handed in from a listing carries that listing's size, mime
    type, and client; a walk has to start from a plain directory rooted on
    the client doing the walking.
    """
    other_client = cast(ListingAccess, object.__new__(Rclone))
    listed_path = RPath(
        remote=Remote(_REMOTE_NAME, other_client),
        path=_DIR_PATH,
        name=_DIR_NAME,
        size=123,
        mime_type="application/octet-stream",
        mod_time="2026-07-27T00:00:00Z",
        is_dir=False,
    )
    listed_path.set_rclone(other_client)

    walk_dir = _to_walk_dir(access, Dir(listed_path))

    assert walk_dir.path.path == _DIR_PATH
    assert walk_dir.path.name == _DIR_NAME
    assert walk_dir.path.size == 0
    assert walk_dir.path.mime_type == _DIRECTORY_MIME_TYPE
    assert walk_dir.path.is_dir is True
    assert walk_dir.path.rclone is access


def test_to_walk_dir_rejects_an_unsupported_source_type(access: ListingAccess) -> None:
    with pytest.raises(TypeError, match="Invalid type for path"):
        _to_walk_dir(access, cast(str, 42))


def test_walk_from_defers_every_coercion_until_iteration_starts(
    access: ListingAccess,
) -> None:
    """Both entry points are generators, so an unsupported `src` surfaces
    on the first `next()` - exactly where it did when these bodies lived
    on `Rclone` itself.
    """
    walking = walk_from(access, cast(str, 42))

    with pytest.raises(TypeError, match="Invalid type for path"):
        next(walking)


def test_scan_missing_folders_from_defers_every_coercion_until_iteration_starts(
    access: ListingAccess, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanned: list[tuple[str, str]] = []

    def record_scan(**kwargs: object) -> Iterator[Dir]:
        scanned.append((str(cast(Dir, kwargs["src"]).path), str(cast(Dir, kwargs["dst"]).path)))
        return iter(())

    monkeypatch.setattr("rclone_kit.operations.traversal_ops.scan_missing_folders", record_scan)

    scanning = scan_missing_folders_from(access, _SRC, _DST)
    assert scanned == []

    assert list(scanning) == []
    assert scanned == [(_SRC, _DST)]
