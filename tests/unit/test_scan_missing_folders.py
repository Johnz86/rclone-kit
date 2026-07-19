"""Unit tests for `rclone_kit.scan_missing_folders.scan_missing_folders`'s
background-thread lifecycle: the walk runs on a daemon `Thread` feeding a
bounded queue, and a consumer that stops iterating early must not leave that
thread blocked forever on a full queue nobody drains.
"""

from collections.abc import Callable
from queue import Queue
from threading import Thread
from typing import cast

import pytest

from rclone_kit import Dir
from rclone_kit import scan_missing_folders as scan_missing_folders_module
from rclone_kit.scan_missing_folders import _MAX_OUT_QUEUE_SIZE, scan_missing_folders
from rclone_kit.types import Order


class _TrackingThread(Thread):
    def __init__(self, *, target: Callable[[], object], daemon: bool) -> None:
        super().__init__(target=target, daemon=daemon)
        _CREATED_THREADS.append(self)


_CREATED_THREADS: list[Thread] = []


def _fake_walk_task_overflowing_queue(
    *, src: object, dst: object, max_depth: int, out_queue: Queue, order: Order
) -> None:
    _ = (src, dst, max_depth, order)
    for i in range(_MAX_OUT_QUEUE_SIZE * 3):
        out_queue.put(f"dir-{i}")
    out_queue.put(None)


@pytest.fixture(autouse=True)
def _track_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    _CREATED_THREADS.clear()
    monkeypatch.setattr(scan_missing_folders_module, "Thread", _TrackingThread)
    monkeypatch.setattr(
        scan_missing_folders_module, "async_diff_dir_walk_task", _fake_walk_task_overflowing_queue
    )


def test_early_break_joins_worker_instead_of_leaking_blocked_thread() -> None:
    generator = scan_missing_folders(src=cast(Dir, "src"), dst=cast(Dir, "dst"), order=Order.NORMAL)

    first = next(generator)
    assert first == "dir-0"

    generator.close()

    assert len(_CREATED_THREADS) == 1
    assert not _CREATED_THREADS[0].is_alive()


def test_normal_exhaustion_yields_everything_and_joins_worker() -> None:
    generator = scan_missing_folders(src=cast(Dir, "src"), dst=cast(Dir, "dst"), order=Order.NORMAL)

    results = list(generator)

    assert results == [f"dir-{i}" for i in range(_MAX_OUT_QUEUE_SIZE * 3)]
    assert len(_CREATED_THREADS) == 1
    assert not _CREATED_THREADS[0].is_alive()
