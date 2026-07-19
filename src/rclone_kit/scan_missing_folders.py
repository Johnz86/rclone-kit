import contextlib
import random
import time
import warnings
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from threading import Thread

from rclone_kit import Dir
from rclone_kit.detail.walk import walk_runner_depth_first
from rclone_kit.dir_listing import DirListing
from rclone_kit.types import ListingOption, Order

_MAX_OUT_QUEUE_SIZE = 50
_WORKER_JOIN_TIMEOUT_SECONDS = 30.0


def _reorder_inplace(data: list, order: Order) -> None:
    if order == Order.NORMAL:
        return
    elif order == Order.REVERSE:
        data.reverse()
        return
    elif order == Order.RANDOM:
        random.shuffle(data)
        return
    else:
        raise ValueError(f"Invalid order: {order}")


def _async_diff_dir_walk_task(
    src: Dir, dst: Dir, max_depth: int, out_queue: Queue[Dir | None], order: Order
) -> None:
    can_scan_two_deep = max_depth > 1 or max_depth == -1
    ls_depth = 2 if can_scan_two_deep else 1
    with ThreadPoolExecutor(max_workers=2) as executor:
        t1 = executor.submit(
            src.ls,
            listing_option=ListingOption.DIRS_ONLY,
            order=order,
            max_depth=ls_depth,
        )
        t2 = executor.submit(
            dst.ls,
            listing_option=ListingOption.DIRS_ONLY,
            order=order,
            max_depth=ls_depth,
        )
        src_dir_listing: DirListing = t1.result()
        dst_dir_listing: DirListing = t2.result()
    next_depth = max_depth - ls_depth if max_depth > 0 else max_depth
    # Each listing's entries must be made relative to their OWN root (not
    # the other side's) - the relative path is what gets compared between
    # src and dst below, independent of their differing absolute prefixes.
    dst_dirs: list[str] = [d.relative_to(dst) for d in dst_dir_listing.dirs]
    src_dirs: list[str] = [d.relative_to(src) for d in src_dir_listing.dirs]
    dst_files_set: set[str] = set(dst_dirs)
    matching_dirs: list[str] = []
    _reorder_inplace(src_dirs, order)
    _reorder_inplace(dst_dirs, order)
    for _i, src_dir in enumerate(src_dirs):
        src_dir_dir = src / src_dir
        if src_dir not in dst_files_set:
            out_queue.put(src_dir_dir)
            if next_depth > 0 or next_depth == -1:
                queue_dir_listing: Queue[DirListing | None] = Queue()
                walk_runner_depth_first(
                    dir=src_dir_dir,
                    out_queue=queue_dir_listing,
                    order=order,
                    max_depth=next_depth,
                )
                while dirlisting := queue_dir_listing.get():
                    if dirlisting is None:
                        break
                    for d in dirlisting.dirs:
                        out_queue.put(d)
        else:
            matching_dirs.append(src_dir)

    for matching_dir in matching_dirs:
        if next_depth > 0 or next_depth == -1:
            src_next = src / matching_dir
            dst_next = dst / matching_dir
            _async_diff_dir_walk_task(
                src=src_next,
                dst=dst_next,
                max_depth=next_depth,
                out_queue=out_queue,
                order=order,
            )


def async_diff_dir_walk_task(
    src: Dir, dst: Dir, max_depth: int, out_queue: Queue[Dir | None], order: Order
) -> None:
    try:
        _async_diff_dir_walk_task(
            src=src, dst=dst, max_depth=max_depth, out_queue=out_queue, order=order
        )
    except Exception:
        import _thread

        _thread.interrupt_main()
        raise
    finally:
        out_queue.put(None)


def _drain_queue_until_sentinel(out_queue: Queue[Dir | None]) -> None:
    """Consume `out_queue` until the walk task's sentinel `None` appears.

    Used when a `scan_missing_folders` consumer stops iterating early
    (`break`, or garbage collection closing the generator) instead of
    letting the walk run to completion. Without this, the background walk
    thread would block forever on `out_queue.put()` once nobody drains its
    bounded queue, leaking a permanently blocked thread.
    """
    with contextlib.suppress(KeyboardInterrupt):
        while out_queue.get() is not None:
            pass


def scan_missing_folders(
    src: Dir,
    dst: Dir,
    max_depth: int = -1,
    order: Order = Order.NORMAL,
) -> Generator[Dir]:
    """Yield every directory present under `src` that is missing under the
    corresponding relative path in `dst`.

    A folder found missing is yielded once for itself; if it has a
    subtree, every descendant directory is yielded too (walked via
    `walk_runner_depth_first`, since a whole missing subtree needs no
    further src/dst comparison - none of it exists on the `dst` side by
    definition). Folders present under `src` and `dst` at a given relative
    path are recursed into, in case they diverge further down.

    Args:
        src: Source directory to walk through
        dst: Destination directory to walk through
        max_depth: Maximum depth to traverse (-1 for unlimited)

    Yields:
        Dir: each directory present under `src` but missing under `dst`
    """

    out_queue: Queue[Dir | None] = Queue(maxsize=_MAX_OUT_QUEUE_SIZE)

    def task() -> None:
        async_diff_dir_walk_task(
            src=src,
            dst=dst,
            max_depth=max_depth,
            out_queue=out_queue,
            order=order,
        )

    worker = Thread(
        target=task,
        daemon=True,
    )
    worker.start()

    sentinel_seen = False
    try:
        while True:
            try:
                dir = out_queue.get_nowait()
            except Empty:
                time.sleep(0.1)
                continue
            if dir is None:
                sentinel_seen = True
                break
            yield dir
    except KeyboardInterrupt:
        pass
    finally:
        if not sentinel_seen:
            _drain_queue_until_sentinel(out_queue)
        worker.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)
        if worker.is_alive():
            warnings.warn(
                "scan_missing_folders background walk did not finish within "
                f"{_WORKER_JOIN_TIMEOUT_SECONDS}s of generator teardown",
                stacklevel=2,
            )
