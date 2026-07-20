from __future__ import annotations

import logging
import os
from collections import OrderedDict
from collections.abc import Generator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rclone_kit.fs.filesystem import FSPath

logger = logging.getLogger(__name__)

_FS_WALK_THREAD_MAX_BACKLOG = int(os.getenv("FS_WALK_THREAD_MAX_BACKLOG", "16"))

# Deliberately a process-lifetime singleton shared by every fs_walk call,
# not an owned/closeable resource: concurrent.futures.thread registers its
# own atexit hook that waits for outstanding work items before interpreter
# shutdown, so this pool cannot leak a blocked or orphaned thread the way a
# hand-rolled one could. Sized once at import time from
# FS_WALK_THREAD_MAX_BACKLOG; changing the env var afterward has no effect.
_executor = ThreadPoolExecutor(max_workers=_FS_WALK_THREAD_MAX_BACKLOG)


def _list_dir(path: FSPath):
    try:
        filenames, dirnames = path.ls()
    except Exception as e:
        logger.warning(f"Unable to list directory {path}: {e}")
        return None
    return path, dirnames, filenames


def fs_walk_parallel(
    self: FSPath,
) -> Generator[tuple[FSPath, list[str], list[str]]]:
    """
    Parallel version of fs_walk: walks `self` and lists
    up to 16 directories at once using the global executor,
    but yields results in the same order tasks were submitted.
    """
    root = self

    futures: OrderedDict = OrderedDict()

    futures[_executor.submit(_list_dir, root)] = root

    while futures:
        done, _ = wait(futures.keys(), return_when=FIRST_COMPLETED)

        for fut in list(futures.keys()):
            if fut not in done:
                continue

            _ = futures.pop(fut)
            result = fut.result()
            if result is None:
                continue

            current_dir, dirnames, filenames = result
            yield current_dir, dirnames, filenames

            for dirname in dirnames:
                sub = current_dir / dirname
                futures[_executor.submit(_list_dir, sub)] = sub


def fs_walk(self: FSPath) -> Generator[tuple[FSPath, list[str], list[str]]]:
    """Sequential API, now backed by the global-thread-pool parallel implementation."""
    yield from fs_walk_parallel(self)
