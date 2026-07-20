"""Unit tests for `rclone_kit.operations.walk`'s `walk_runner_depth_first` and
`walk_runner_breadth_first`, run entirely offline against an in-memory fake
directory tree (no subprocess or network calls).

Neither function had any unit test coverage before this file: the only
caller-side coverage was `tests/cloud/test_walk.py`, which is gated behind
real cloud credentials and only ever prints results without asserting
completeness - it could not have caught either bug fixed here.

Each walker enqueues exactly one `DirListing` per directory it visits
(that directory's own children, not the directory itself), so a tree with
N total directories should produce N queued listings, and the union of
every listing's child names should equal every non-root directory in the
tree (the root is nobody's child).
"""

from queue import Queue
from typing import cast

import pytest

from rclone_kit.client import Rclone
from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.operations.walk import walk, walk_runner_breadth_first, walk_runner_depth_first
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath
from rclone_kit.types import Order

# root -> A -> A1, A2 (A2 -> A2a)
#      -> B -> B1
_TREE = {"A": {"A1": {}, "A2": {"A2a": {}}}, "B": {"B1": {}}}
_ALL_NON_ROOT_NAMES = {"A", "A1", "A2", "A2a", "B", "B1"}
_TOTAL_DIR_COUNT = len(_ALL_NON_ROOT_NAMES) + 1  # + root itself


class _FakeTreeRclone:
    """Fakes just enough of `Rclone.ls` to drive `Dir.ls()` against an
    in-memory nested-dict directory tree. Only ever lists one level at a
    time (`.dirs` = immediate children of the requested path), matching how
    both walkers actually call `Dir.ls()`.
    """

    def __init__(self, tree: dict) -> None:
        self.remote = Remote(name="remote", rclone=cast(Rclone, self))
        self._tree = tree

    def root(self) -> Dir:
        rpath = RPath(
            remote=self.remote,
            path="root",
            name="root",
            size=0,
            mime_type="inode/directory",
            mod_time="",
            is_dir=True,
        )
        rpath.set_rclone(cast(Rclone, self))
        return Dir(rpath)

    def _subtree(self, path: str) -> dict:
        node = self._tree
        for part in path.split("/")[1:]:
            node = node[part]
        return node

    def ls(self, src: Dir, *_args, **_kwargs) -> DirListing:
        node = self._subtree(src.path.path)
        rpaths: list[RPath] = []
        for name in node:
            rpath = RPath(
                remote=self.remote,
                path=f"{src.path.path}/{name}",
                name=name,
                size=0,
                mime_type="inode/directory",
                mod_time="",
                is_dir=True,
            )
            rpath.set_rclone(cast(Rclone, self))
            rpaths.append(rpath)
        return DirListing(rpaths)


def _drain(out_queue: Queue[DirListing | None]) -> list[DirListing]:
    listings: list[DirListing] = []
    while (item := out_queue.get_nowait()) is not None:
        listings.append(item)
    assert out_queue.empty(), "extra items queued after the sentinel None"
    return listings


@pytest.mark.parametrize(
    "walker",
    [walk_runner_depth_first, walk_runner_breadth_first],
    ids=["depth_first", "breadth_first"],
)
def test_walker_visits_every_directory_in_a_multi_branch_tree(walker) -> None:
    root = _FakeTreeRclone(_TREE).root()
    out_queue: Queue[DirListing | None] = Queue()

    walker(root, max_depth=-1, out_queue=out_queue, order=Order.NORMAL)

    listings = _drain(out_queue)
    assert len(listings) == _TOTAL_DIR_COUNT

    discovered_names = {d.name for listing in listings for d in listing.dirs}
    assert discovered_names == _ALL_NON_ROOT_NAMES


@pytest.mark.parametrize(
    "walker",
    [walk_runner_depth_first, walk_runner_breadth_first],
    ids=["depth_first", "breadth_first"],
)
def test_walker_respects_max_depth(walker) -> None:
    """max_depth=1 visits root and its direct children (A, B) but not
    their descendants.
    """
    root = _FakeTreeRclone(_TREE).root()
    out_queue: Queue[DirListing | None] = Queue()

    walker(root, max_depth=1, out_queue=out_queue, order=Order.NORMAL)

    listings = _drain(out_queue)
    assert len(listings) == 3  # root, A, B

    discovered_names = {d.name for listing in listings for d in listing.dirs}
    assert discovered_names == {"A", "A1", "A2", "B", "B1"}


@pytest.mark.parametrize("breadth_first", [True, False], ids=["breadth_first", "depth_first"])
def test_walk_generator_yields_every_directory(breadth_first: bool) -> None:
    """End-to-end through the public `walk()` generator (the background
    `Thread` + blocking `out_queue.get()` consumer that `Rclone.walk`
        actually uses), not just the runner functions directly.
    """
    root = _FakeTreeRclone(_TREE).root()

    listings = list(walk(root, breadth_first=breadth_first, max_depth=-1))

    assert len(listings) == _TOTAL_DIR_COUNT
    discovered_names = {d.name for listing in listings for d in listing.dirs}
    assert discovered_names == _ALL_NON_ROOT_NAMES
