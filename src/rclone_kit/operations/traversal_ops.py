"""Client-bound entry points for the two recursive traversals.

`Rclone.walk()` and `Rclone.scan_missing_folders()` accept
`Dir | Remote | str`, while the traversal engines behind them
(`operations/walk.walk` and `scan_missing_folders.scan_missing_folders`)
take `Dir` values only. Turning one into the other is the client-bound
half of those operations - it has to bind the resulting paths to a
client - so it lives here, leaving both engines free of any client
dependency.

Both functions are generators, so a caller pays for the conversion (and
sees a `TypeError` for an unsupported `src` type) only once iteration
actually starts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rclone_kit.dir import Dir
from rclone_kit.operations.walk import walk
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath
from rclone_kit.scan_missing_folders import scan_missing_folders
from rclone_kit.types import Order
from rclone_kit.util import to_path

if TYPE_CHECKING:
    from collections.abc import Generator

    from rclone_kit.access import ListingAccess
    from rclone_kit.dir_listing import DirListing

_DIRECTORY_MIME_TYPE = "inode/directory"


def _to_walk_dir(access: ListingAccess, src: Dir | Remote | str) -> Dir:
    """Bind `src` to `access` as the directory a walk starts from.

    A `Dir` is rebuilt around a fresh directory `RPath` instead of being
    reused as-is: the incoming value may carry listing-derived metadata
    (a real size, a file mime type) or another client, and a walk must
    start from a plain directory rooted on *this* client.
    """
    if isinstance(src, Dir):
        rpath = RPath(
            remote=src.remote,
            path=src.path.path,
            name=src.path.name,
            size=0,
            mime_type=_DIRECTORY_MIME_TYPE,
            mod_time="",
            is_dir=True,
        )
        rpath.set_rclone(access)
        return Dir(rpath)
    elif isinstance(src, str):
        return Dir(to_path(src, access))
    elif isinstance(src, Remote):
        return Dir(src)
    else:
        raise TypeError(f"Invalid type for path: {type(src)}")


def walk_from(
    access: ListingAccess,
    src: Dir | Remote | str,
    max_depth: int = -1,
    breadth_first: bool = True,
    order: Order = Order.NORMAL,
) -> Generator[DirListing]:
    """Walk `src` recursively, yielding one `DirListing` per directory."""
    yield from walk(
        _to_walk_dir(access, src),
        max_depth=max_depth,
        breadth_first=breadth_first,
        order=order,
    )


def scan_missing_folders_from(
    access: ListingAccess,
    src: Dir | Remote | str,
    dst: Dir | Remote | str,
    max_depth: int = -1,
    order: Order = Order.NORMAL,
) -> Generator[Dir]:
    """Yield every directory under `src` missing at the matching path
    under `dst`; see `Rclone.scan_missing_folders()` for the full
    contract.

    Both sides go through `to_path` rather than `_to_walk_dir`: this
    traversal compares two trees by relative path and never needs the
    starting directories reduced to bare directory `RPath`s.
    """
    src_dir = Dir(to_path(src, access))
    dst_dir = Dir(to_path(dst, access))
    yield from scan_missing_folders(src=src_dir, dst=dst_dir, max_depth=max_depth, order=order)
