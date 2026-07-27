from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import Generator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rclone_kit.fs.filesystem import FSPath

logger = logging.getLogger(__name__)

type _Listing = tuple[FSPath, list[str], list[str]]

_FS_WALK_THREAD_MAX_BACKLOG = int(os.getenv("FS_WALK_THREAD_MAX_BACKLOG", "16"))

# Deliberately a process-lifetime singleton shared by every fs_walk call,
# not an owned/closeable resource: concurrent.futures.thread registers its
# own atexit hook that waits for outstanding work items before interpreter
# shutdown, so this pool cannot leak a blocked or orphaned thread the way a
# hand-rolled one could. Sized once at import time from
# FS_WALK_THREAD_MAX_BACKLOG; changing the env var afterward has no effect.
_executor = ThreadPoolExecutor(max_workers=_FS_WALK_THREAD_MAX_BACKLOG)

# How many listings a single walk may have submitted-but-not-yet-consumed.
# Reuses the pool's own size rather than adding a second knob: one
# outstanding listing per worker thread means the executor's *unbounded*
# internal work queue never holds anything, so this bound alone bounds a
# walk's memory. Named separately from the pool size because it is a
# different concept - a backlog limit, not a thread count - and because
# every call site below reads better for saying which one it means.
_FS_WALK_MAX_OUTSTANDING_LISTINGS = _FS_WALK_THREAD_MAX_BACKLOG


def _list_dir(path: FSPath) -> _Listing | None:
    """List one directory, or log and skip it on failure rather than
    aborting the whole walk.

    Deliberately different from `rclone_kit.operations.walk`'s RC-backed
    walker, which propagates a listing failure to the caller instead:
    `FSPath`/`RealFS` walks a local (or already-open remote) filesystem,
    where a single permission-denied or since-deleted subdirectory
    partway through a large tree is the common, expected failure mode
    (the same default `os.walk` itself takes) - not a sign the whole
    source is unreachable, which is the failure `operations.walk` is
    built to surface loudly instead.
    """
    try:
        filenames, dirnames = path.ls()
    except Exception as e:
        logger.warning(f"Unable to list directory {path}: {e}")
        return None
    return path, dirnames, filenames


def _submit_pending(pending: deque[FSPath], in_flight: deque[Future[_Listing | None]]) -> None:
    """Top `in_flight` back up to `_FS_WALK_MAX_OUTSTANDING_LISTINGS`.

    The single place the backlog bound is enforced, and it is enforced by
    *not submitting* rather than by blocking anything - see
    `fs_walk_parallel`'s deadlock argument.
    """
    while pending and len(in_flight) < _FS_WALK_MAX_OUTSTANDING_LISTINGS:
        in_flight.append(_executor.submit(_list_dir, pending.popleft()))


def fs_walk_parallel(
    self: FSPath,
) -> Generator[tuple[FSPath, list[str], list[str]]]:
    """Walk `self` breadth-first, listing up to
    `_FS_WALK_MAX_OUTSTANDING_LISTINGS` directories at once on the shared
    executor and yielding them in the order they were submitted.

    Ordering. Directories are submitted in discovery order and consumed
    head-first from that same FIFO, so a parent is always yielded before
    any of its children and siblings keep their `FS.ls()` order. The
    previous implementation instead rescanned every pending future after
    each completion and yielded whichever ones happened to be finished,
    so a fast sibling overtook a slow one: its only real guarantee was
    "parent before child", and its docstring's claim of submission order
    was aspirational. Head-first consumption is one of the orders that
    implementation could already produce, so nothing that worked against
    it breaks; it is now simply the only one, deterministically. The
    callers only ever needed the weaker guarantee - `FSPath.rmtree`
    deletes each directory's files as they arrive, and `FSPath.walk`'s
    consumers assert set membership - so tightening it costs them
    nothing.

    Backlog. Every discovered subdirectory used to be submitted the
    moment it was seen, so the pending-future map held one entry - and
    eventually one materialised listing - per directory in the entire
    tree. `max_workers` bounds concurrency, not the executor's own
    unbounded work queue, so a wide remote tree grew memory and queued RC
    listings without limit. Now at most
    `_FS_WALK_MAX_OUTSTANDING_LISTINGS` listings exist at once;
    discovered-but-unvisited directories wait in `pending` as bare
    `FSPath` references, which any walker must remember regardless and
    which cost a fraction of a materialised listing. Refilling happens
    before each `yield` so the pool keeps listing while the consumer
    processes the directory it was just handed.

    Deadlock freedom. `_list_dir` only calls `path.ls()` and returns; no
    worker ever waits on this generator, so every submitted future
    resolves whether or not the consumer asks for another item. This
    generator blocks only in `Future.result()` on the FIFO head, which is
    always a task that is running or queued behind tasks that likewise
    cannot block. Nothing is bounded by blocking a producer, so the
    "submitted task waits for the consumer while the consumer waits for
    that task" failure mode cannot arise. Abandoning the generator leaves
    at most `_FS_WALK_MAX_OUTSTANDING_LISTINGS` tasks, which still run to
    completion and are then collected, so the executor's atexit hook can
    never be left waiting on a blocked worker.

    Cost. O(1) work per completed listing: no rescan of the pending set,
    unlike the previous `wait(futures.keys())` plus full-rescan loop that
    made a walk quadratic in the number of directories.
    """
    pending: deque[FSPath] = deque([self])
    in_flight: deque[Future[_Listing | None]] = deque()

    while True:
        _submit_pending(pending, in_flight)
        if not in_flight:
            return

        listing = in_flight.popleft().result()
        if listing is None:
            continue

        current_dir, dirnames, filenames = listing
        pending.extend(current_dir / dirname for dirname in dirnames)
        _submit_pending(pending, in_flight)
        yield current_dir, dirnames, filenames


def fs_walk(self: FSPath) -> Generator[tuple[FSPath, list[str], list[str]]]:
    """Sequential API, now backed by the global-thread-pool parallel implementation."""
    yield from fs_walk_parallel(self)
