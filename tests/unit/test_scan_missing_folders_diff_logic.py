"""Unit tests for the actual directory-diff logic inside
`rclone_kit.scan_missing_folders._async_diff_dir_walk_task`, run entirely
offline against an in-memory fake directory tree (no subprocess or network
calls).

`test_scan_missing_folders.py` covers the outer generator's background-
thread lifecycle by faking out `async_diff_dir_walk_task` entirely, so it
never exercises this logic; `tests/cloud/test_scan_missing_folders.py` is
`@unittest.skip`'d and, even when enabled, only ever compared a remote to
itself (`src == dst == "dst:rclone-kit-unit-test"`). That trivial
self-comparison could never have caught the bug fixed here:
`_async_diff_dir_walk_task` compared each listing's entries relative to
the *other* side's root (`d.relative_to(src)` for `dst`'s own listing, and
vice versa) instead of its own root, so `PurePosixPath.relative_to` would
only succeed by accident when `src` and `dst` happened to share a path
prefix - exactly what a self-comparison test guarantees, masking the bug
completely.
"""

from typing import cast

from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.rclone_impl import RcloneImpl
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath
from rclone_kit.scan_missing_folders import scan_missing_folders
from rclone_kit.types import ListingOption, Order


class _FakeTreeRclone:
    """Fakes just enough of `RcloneImpl.ls` to drive `Dir.ls()` against an
    in-memory nested-dict directory tree, including rclone's real
    `lsjson --max-depth N` behavior of returning a flattened listing of
    every directory within N levels (not just the immediate children) -
    `_async_diff_dir_walk_task` relies on that flattening when it requests
    `max_depth=2` to fetch two tree levels in one round trip.
    """

    def __init__(self, remote_name: str, tree: dict) -> None:
        self.remote = Remote(name=remote_name, rclone=cast(RcloneImpl, self))
        self._tree = tree

    def root(self) -> Dir:
        rpath = RPath(
            remote=self.remote,
            path="bucket",
            name="bucket",
            size=0,
            mime_type="inode/directory",
            mod_time="",
            is_dir=True,
        )
        rpath.set_rclone(cast(RcloneImpl, self))
        return Dir(rpath)

    def _subtree(self, path: str) -> dict:
        node = self._tree
        for part in path.split("/")[1:]:
            node = node[part]
        return node

    def ls(
        self,
        src: Dir,
        max_depth: int | None = None,
        glob: str | None = None,
        order: Order = Order.NORMAL,
        listing_option: ListingOption = ListingOption.ALL,
    ) -> DirListing:
        del glob, listing_option, order
        base_path = src.path.path
        node = self._subtree(base_path)
        depth_limit: int | None
        if max_depth is None:
            depth_limit = 1
        elif max_depth < 0:
            depth_limit = None
        else:
            depth_limit = max_depth

        rpaths: list[RPath] = []

        def _walk(subnode: dict, prefix: str, remaining: int | None) -> None:
            for name, children in subnode.items():
                child_path = f"{prefix}/{name}"
                rpath = RPath(
                    remote=self.remote,
                    path=child_path,
                    name=name,
                    size=0,
                    mime_type="inode/directory",
                    mod_time="",
                    is_dir=True,
                )
                rpath.set_rclone(cast(RcloneImpl, self))
                rpaths.append(rpath)
                if remaining is None or remaining > 1:
                    next_remaining = None if remaining is None else remaining - 1
                    _walk(children, child_path, next_remaining)

        _walk(node, base_path, depth_limit)
        return DirListing(rpaths)


def test_scan_missing_folders_finds_top_level_and_nested_missing_dirs() -> None:
    """src has B (with descendant B1) that dst lacks entirely, and A/A1
    where A matches but A1 is missing under dst's A.
    """
    src_tree = {"A": {"A1": {}}, "B": {"B1": {}}}
    dst_tree = {"A": {}}
    src_root = _FakeTreeRclone("src", src_tree).root()
    dst_root = _FakeTreeRclone("dst", dst_tree).root()

    missing = list(scan_missing_folders(src_root, dst_root, max_depth=-1))

    missing_paths = {d.path.path for d in missing}
    assert missing_paths == {"bucket/B", "bucket/B/B1", "bucket/A/A1"}


def test_scan_missing_folders_no_differences_yields_nothing() -> None:
    tree = {"A": {"A1": {}}, "B": {}}
    src_root = _FakeTreeRclone("src", tree).root()
    dst_root = _FakeTreeRclone("dst", tree).root()

    missing = list(scan_missing_folders(src_root, dst_root, max_depth=-1))

    assert missing == []


def test_scan_missing_folders_respects_max_depth() -> None:
    """With max_depth=1, only top-level missing directories are reported -
    B is found missing, but its descendant B1 is not walked into.
    """
    src_tree = {"A": {}, "B": {"B1": {}}}
    dst_tree = {"A": {}}
    src_root = _FakeTreeRclone("src", src_tree).root()
    dst_root = _FakeTreeRclone("dst", dst_tree).root()

    missing = list(scan_missing_folders(src_root, dst_root, max_depth=1))

    missing_paths = {d.path.path for d in missing}
    assert missing_paths == {"bucket/B"}
