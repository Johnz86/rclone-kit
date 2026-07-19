"""Unit tests for `rclone_kit.fs.walk_threaded.FSWalkThread` and
`rclone_kit.fs.walk_threaded_walker.FSWalker`'s resource-ownership
lifecycle: an idempotent `close()` reachable outside the context-manager
protocol, that also unblocks a worker thread currently stuck inside a full
`result_queue.put()` instead of leaking it.
"""

from typing import cast

import pytest

from rclone_kit.fs.filesystem import FSPath
from rclone_kit.fs.walk_threaded import FSWalkThread
from rclone_kit.fs.walk_threaded_walker import FSWalker


def _fake_fs_walk_empty(_fspath: FSPath):
    return iter(())


def _fake_fs_walk_many(_fspath: FSPath):
    for i in range(20):
        yield (cast(FSPath, f"root-{i}"), [], [])


def test_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rclone_kit.fs.walk.fs_walk", _fake_fs_walk_empty)
    walker = FSWalkThread(cast(FSPath, "root"))

    walker.close()
    walker.close()

    assert not walker.thread.is_alive()


def test_close_unblocks_worker_blocked_on_full_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rclone_kit.fs.walk.fs_walk", _fake_fs_walk_many)
    walker = FSWalkThread(cast(FSPath, "root"), max_backlog=1)

    walker.close()

    assert not walker.thread.is_alive()


def test_fswalker_close_before_enter_is_a_noop() -> None:
    walker = FSWalker(fspath=cast(FSPath, "root"), max_backlog=8)

    walker.close()


def test_fswalker_exit_stops_worker_even_when_not_fully_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rclone_kit.fs.walk.fs_walk", _fake_fs_walk_many)

    with FSWalker(fspath=cast(FSPath, "root"), max_backlog=1) as walker:
        next(iter(walker))

    assert not walker.walker.thread.is_alive()
