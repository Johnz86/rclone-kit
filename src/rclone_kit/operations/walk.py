import contextlib
import random
import warnings
from collections.abc import Generator
from queue import Queue
from threading import Thread

from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.remote import Remote
from rclone_kit.types import Order

_MAX_OUT_QUEUE_SIZE = 50
_WORKER_JOIN_TIMEOUT_SECONDS = 30.0


def walk_runner_breadth_first(
    dir: Dir,
    max_depth: int,
    out_queue: Queue[DirListing | None],
    order: Order = Order.NORMAL,
) -> None:
    """Breadth-first counterpart to `walk_runner_depth_first`.

    Tracks remaining depth per queued node (`(Dir, depth)` tuples), not a
    single `max_depth` counter decremented once per node dequeued: the
    previous implementation exhausted that shared counter after processing
    just one node per remaining depth level, regardless of how many
    siblings existed at that level, so only the first branch was ever
    walked past depth 1 - e.g. with three top-level children and
    max_depth=2, only the first child's own children were visited; the
    second and third children's subdirectories were silently skipped.

    Always puts a `None` sentinel before returning, success or failure -
    `walk()`'s consumer loop blocks on `out_queue.get()` forever otherwise.
    Any exception simply propagates to whoever called this function - a
    synchronous caller (e.g. `scan_missing_folders`) sees it directly;
    `walk()` runs this as a background thread's target and captures it
    itself to re-raise from its own consumer loop.
    """
    queue: Queue[tuple[Dir, int]] = Queue()
    queue.put((dir, max_depth))
    try:
        while not queue.empty():
            current_dir, depth = queue.get()
            dirlisting = current_dir.ls(max_depth=0, order=order)
            out_queue.put(dirlisting)

            if depth != 0:
                next_depth = depth - 1 if depth > 0 else depth
                for child in dirlisting.dirs:
                    queue.put((child, next_depth))
    finally:
        out_queue.put(None)


def walk_runner_depth_first(
    dir: Dir,
    max_depth: int,
    out_queue: Queue[DirListing | None],
    order: Order = Order.NORMAL,
) -> None:
    """Depth-first counterpart to `walk_runner_breadth_first`.

    Uses a single iterative stack, not recursive self-calls: the previous
    implementation recursed directly (`walk_runner_depth_first(subdir, ...)`)
    instead of pushing onto `stack`, so every recursive call independently
    put its own `None` sentinel onto the shared `out_queue`. The first
    consumer (`walk()`'s `while ... : if dirlisting is None: break`) stopped
    at the first sentinel it saw, silently truncating the walk to whatever
    the first-visited subtree had produced - anything from later siblings,
    or from the starting directory's own listing, was left unread in the
    queue. Pushing subdirectories onto the shared stack instead means
    exactly one sentinel is put, after the entire traversal completes.

    Each directory's listing is put onto `out_queue` before its
    subdirectories are pushed (pre-order), matching
    `walk_runner_breadth_first`'s ordering.

    Always puts a `None` sentinel before returning, success or failure - see
    `walk_runner_breadth_first`'s docstring for why, and for how a failure
    reaches the caller.
    """
    try:
        stack = [(dir, max_depth)]
        while stack:
            current_dir, depth = stack.pop()
            dirlisting = current_dir.ls()
            if order == Order.REVERSE:
                dirlisting.dirs.reverse()
            if order == Order.RANDOM:
                random.shuffle(dirlisting.dirs)
            out_queue.put(dirlisting)
            if depth != 0:
                next_depth = depth - 1 if depth > 0 else depth
                stack.extend((subdir, next_depth) for subdir in reversed(dirlisting.dirs))
    finally:
        out_queue.put(None)


def _drain_queue_until_sentinel(out_queue: Queue[DirListing | None]) -> None:
    """Consume `out_queue` until the walk runner's sentinel `None` appears.

    Used when a `walk()` consumer stops iterating early (`break`, or
    garbage collection closing the generator) instead of letting the walk
    run to completion. Without this, the background walk thread would
    block forever on `out_queue.put()` once nobody drains its bounded
    queue, leaking a permanently blocked thread.
    """
    with contextlib.suppress(KeyboardInterrupt):
        while out_queue.get() is not None:
            pass


def walk(
    dir: Dir | Remote,
    breadth_first: bool,
    max_depth: int = -1,
    order: Order = Order.NORMAL,
) -> Generator[DirListing]:
    """Walk through the given directory recursively.

    Args:
        dir: Directory or Remote to walk through
        max_depth: Maximum depth to traverse (-1 for unlimited)

    Yields:
        DirListing: Directory listing for each directory encountered
    """
    if isinstance(dir, Remote):
        dir = Dir(dir)
    out_queue: Queue[DirListing | None] = Queue(maxsize=_MAX_OUT_QUEUE_SIZE)
    errors: list[BaseException] = []

    def _task() -> None:
        try:
            if breadth_first:
                walk_runner_breadth_first(dir, max_depth, out_queue, order)
            else:
                walk_runner_depth_first(dir, max_depth, out_queue, order)
        except BaseException as exc:
            # Captured here and re-raised from the consumer loop below,
            # rather than via _thread.interrupt_main(): that call schedules
            # an async KeyboardInterrupt delivered at the main thread's next
            # bytecode boundary, which can land after this generator has
            # already exhausted normally (sentinel already consumed) -
            # surfacing as a misleading KeyboardInterrupt in unrelated later
            # code instead of this real failure.
            errors.append(exc)

    worker = Thread(
        target=_task,
        daemon=True,
    )
    worker.start()

    sentinel_seen = False
    try:
        while True:
            dirlisting = out_queue.get()
            if dirlisting is None:
                sentinel_seen = True
                break
            yield dirlisting
    except KeyboardInterrupt:
        pass
    finally:
        if not sentinel_seen:
            _drain_queue_until_sentinel(out_queue)
        worker.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)
        if worker.is_alive():
            warnings.warn(
                "walk background thread did not finish within "
                f"{_WORKER_JOIN_TIMEOUT_SECONDS}s of generator teardown",
                stacklevel=2,
            )
    if errors:
        raise errors[0]
