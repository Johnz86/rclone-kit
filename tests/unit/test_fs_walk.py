"""Unit tests for `rclone_kit.fs.walk`'s parallel `FSPath` walker, run
entirely against an in-memory fake `FS` (no disk, subprocess or network).

They pin the two properties the walker exists to have, not merely that it
still returns every directory:

- the number of listings submitted to the shared executor but not yet
  consumed stays bounded, so a very wide tree cannot grow memory or the
  executor's own unbounded work queue without limit;
- directories come out in submission (breadth-first) order, each exactly
  once, even when a later-submitted sibling finishes listing first.

Neither property survives the obvious implementation - submitting every
subdirectory the moment it is discovered, and yielding whichever pending
futures happen to be done - so both are asserted directly rather than
inferred from a complete result set.
"""

import logging
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

import pytest

from rclone_kit.fs import walk
from rclone_kit.fs.filesystem import FSPath, RealFS
from rclone_kit.fs.walk import _FS_WALK_MAX_OUTSTANDING_LISTINGS, fs_walk

_ROOT = "root"
_FILE_NAME = "f.txt"
_UNLISTABLE_MESSAGE = "simulated permission failure"
_SLOW_LISTING_SECONDS = 0.05

_ORDERED_TREE: dict[str, tuple[list[str], list[str]]] = {
    "root": ([_FILE_NAME], ["a", "b"]),
    "root/a": ([], ["a1", "a2"]),
    "root/b": ([_FILE_NAME], ["b1"]),
    "root/a/a1": ([_FILE_NAME], []),
    "root/a/a2": ([], []),
    "root/b/b1": ([], []),
}
_EXPECTED_BFS_ORDER = ["root", "root/a", "root/b", "root/a/a1", "root/a/a2", "root/b/b1"]

# "root/a" is submitted before its sibling "root/b" but listed far more
# slowly, which is precisely the case the old completion-order loop got
# wrong.
_SLOW_PATH = "root/a"

_UNLISTABLE_PATH = "root/a"
_UNLISTABLE_SUBTREE = {"root/a", "root/a/a1", "root/a/a2"}
_EXPECTED_ORDER_WITHOUT_UNLISTABLE_SUBTREE = [
    path for path in _EXPECTED_BFS_ORDER if path not in _UNLISTABLE_SUBTREE
]


class _FakeTreeFS(RealFS):
    """A `RealFS` whose `ls()` reads an in-memory `path -> (files, dirs)`
    map instead of the disk.

    Subclasses `RealFS` rather than implementing `FS` from scratch because
    `FSPath` branches on `isinstance(self.fs, RemoteFS)` for its path
    math: any other concrete `FS` makes `current_dir / name` join with
    local semantics, which is all `fs_walk` needs from the filesystem
    besides `ls()`.
    """

    def __init__(
        self,
        listings: dict[str, tuple[list[str], list[str]]],
        unlistable: frozenset[str] = frozenset(),
        slow_paths: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self.listings = listings
        self.unlistable = unlistable
        self.slow_paths = slow_paths

    def ls(self, path: Path | str) -> tuple[list[str], list[str]]:
        key = str(path)
        if key in self.slow_paths:
            time.sleep(_SLOW_LISTING_SECONDS)
        if key in self.unlistable:
            raise PermissionError(_UNLISTABLE_MESSAGE)
        return self.listings[key]


class _RecordingExecutor:
    """Stands in for the module's shared pool, running each listing inline
    and recording the high-water mark of listings that were submitted but
    whose result the walk had not yet retrieved.

    That difference is exactly the backlog `fs_walk` must bound, and
    running inline makes it observable without any timing dependence.
    """

    def __init__(self) -> None:
        self.submitted = 0
        self.retrieved = 0
        self.max_outstanding = 0

    def submit(self, fn, path):
        future = _RecordingFuture(self)
        future.set_result(fn(path))
        self.submitted += 1
        self.max_outstanding = max(self.max_outstanding, self.submitted - self.retrieved)
        return future


class _RecordingFuture(Future[tuple[FSPath, list[str], list[str]] | None]):
    def __init__(self, recorder: _RecordingExecutor) -> None:
        super().__init__()
        self.recorder = recorder

    def result(self, timeout: float | None = None):
        self.recorder.retrieved += 1
        return super().result(timeout)


def _uniform_tree(
    width: int, depth: int
) -> tuple[dict[str, tuple[list[str], list[str]]], list[str]]:
    """Listings for a perfectly uniform tree of the given width and depth,
    together with the breadth-first order its directories must be yielded
    in - built by the same level-order expansion the walker performs.
    """
    listings: dict[str, tuple[list[str], list[str]]] = {}
    order: list[str] = []
    frontier = [_ROOT]
    for level in range(depth + 1):
        next_frontier: list[str] = []
        for path in frontier:
            dirnames = [] if level == depth else [f"d{index}" for index in range(width)]
            listings[path] = ([_FILE_NAME], dirnames)
            order.append(path)
            next_frontier.extend(f"{path}/{dirname}" for dirname in dirnames)
        frontier = next_frontier
    return listings, order


def _walked_paths(fs: _FakeTreeFS) -> list[str]:
    return [str(current_dir) for current_dir, _, _ in fs_walk(FSPath(fs, _ROOT))]


@dataclass(frozen=True)
class TreeShape:
    width: int
    depth: int


WIDE_SHALLOW_TREE = TreeShape(width=200, depth=1)
NARROW_DEEP_TREE = TreeShape(width=1, depth=60)
WIDE_AND_DEEP_TREE = TreeShape(width=5, depth=4)

TREE_SHAPES = [WIDE_SHALLOW_TREE, NARROW_DEEP_TREE, WIDE_AND_DEEP_TREE]


@pytest.mark.parametrize(
    "shape",
    TREE_SHAPES,
    ids=["wide_shallow_tree", "narrow_deep_tree", "wide_and_deep_tree"],
)
def test_outstanding_listings_stay_bounded(shape: TreeShape, monkeypatch: pytest.MonkeyPatch):
    """The walker never has more than
    `_FS_WALK_MAX_OUTSTANDING_LISTINGS` listings submitted-but-unconsumed,
    however wide or deep the tree, while still visiting every directory
    exactly once.
    """
    listings, expected_order = _uniform_tree(shape.width, shape.depth)
    recorder = _RecordingExecutor()
    monkeypatch.setattr(walk, "_executor", recorder)

    walked = _walked_paths(_FakeTreeFS(listings))

    assert walked == expected_order
    assert recorder.submitted == len(expected_order)
    assert recorder.max_outstanding <= _FS_WALK_MAX_OUTSTANDING_LISTINGS


def test_yield_order_is_submission_order_when_a_later_sibling_lists_first():
    """A slow directory must not be overtaken by a sibling submitted after
    it: the old loop rescanned pending futures and yielded whichever were
    done, so `root/b` came out before `root/a`.
    """
    fs = _FakeTreeFS(_ORDERED_TREE, slow_paths=frozenset({_SLOW_PATH}))

    walked = _walked_paths(fs)

    assert walked == _EXPECTED_BFS_ORDER


def test_walk_yields_each_directory_with_its_own_files_and_subdirectories():
    fs = _FakeTreeFS(_ORDERED_TREE)

    walked = {
        str(current_dir): (filenames, dirnames)
        for current_dir, dirnames, filenames in fs_walk(FSPath(fs, _ROOT))
    }

    assert walked == _ORDERED_TREE


def test_an_unlistable_directory_is_skipped_without_aborting_the_walk(
    caplog: pytest.LogCaptureFixture,
):
    """A permission-denied subdirectory costs its own subtree, nothing
    else - `_list_dir` logs and skips rather than propagating, unlike
    `rclone_kit.operations.walk`.
    """
    fs = _FakeTreeFS(_ORDERED_TREE, unlistable=frozenset({_UNLISTABLE_PATH}))

    with caplog.at_level(logging.WARNING, logger=walk.__name__):
        walked = _walked_paths(fs)

    assert walked == _EXPECTED_ORDER_WITHOUT_UNLISTABLE_SUBTREE
    assert _UNLISTABLE_PATH in caplog.text
    assert _UNLISTABLE_MESSAGE in caplog.text


def test_walking_a_directory_that_cannot_be_listed_at_all_yields_nothing(
    caplog: pytest.LogCaptureFixture,
):
    fs = _FakeTreeFS(_ORDERED_TREE, unlistable=frozenset({_ROOT}))

    with caplog.at_level(logging.WARNING, logger=walk.__name__):
        walked = _walked_paths(fs)

    assert walked == []
    assert _UNLISTABLE_MESSAGE in caplog.text
