import random
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

from rclone_kit.background_producer import iter_background_producer
from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.operations.walk import walk
from rclone_kit.types import ListingOption, Order


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


def _relative_names(listing: DirListing, root: Dir) -> list[str]:
    """Names of `listing`'s directories relative to their OWN root.

    That relative path is what gets compared between `src` and `dst`, so
    each side has to be made relative to its own root: their absolute
    prefixes differ by definition, and only the tails are comparable.
    """
    return [d.relative_to(root) for d in listing.dirs]


def _put_missing_subtree(
    root: Dir, max_depth: int, out_queue: Queue[Dir | None], order: Order
) -> None:
    """Put every descendant directory of an already-missing `root` onto
    `out_queue`.

    No src/dst comparison happens below a missing directory: none of that
    subtree exists on the `dst` side by definition, so the whole thing is
    reported by walking `root` alone.

    Goes through `walk()` rather than driving `walk_runner_depth_first`
    into a local queue, because the runner fills its queue synchronously
    and returns only once the entire subtree is in it - materialising
    every listing of the subtree at once. That queue cannot simply be
    bounded either: a bounded queue would block the runner's own `put()`
    in this very thread, with no consumer running yet, and deadlock.
    `walk()` runs the runner on its own thread behind a bounded queue and
    re-raises the runner's failures to this caller.
    """
    for dirlisting in walk(root, breadth_first=False, max_depth=max_depth, order=order):
        for d in dirlisting.dirs:
            out_queue.put(d)


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
    dst_dirs: set[str] = set(_relative_names(dst_dir_listing, dst))
    src_dirs: list[str] = _relative_names(src_dir_listing, src)
    matching_dirs: list[str] = []
    _reorder_inplace(src_dirs, order)
    for src_dir in src_dirs:
        src_dir_dir = src / src_dir
        if src_dir not in dst_dirs:
            out_queue.put(src_dir_dir)
            if next_depth > 0 or next_depth == -1:
                _put_missing_subtree(src_dir_dir, next_depth, out_queue, order)
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
    finally:
        out_queue.put(None)


def scan_missing_folders(
    src: Dir,
    dst: Dir,
    max_depth: int = -1,
    order: Order = Order.NORMAL,
) -> Generator[Dir]:
    """Yield every directory present under `src` that is missing under the
    corresponding relative path in `dst` (`max_depth=-1` for unlimited).

    A folder found missing is yielded once for itself, then its whole
    subtree is walked and every descendant yielded too - see
    `_put_missing_subtree`. Folders present under both `src` and `dst` at
    a given relative path are recursed into, in case they diverge further
    down.

    The diff runs on a background thread feeding a bounded queue, so
    directories are yielded as they are found rather than after the whole
    tree has been compared - see `iter_background_producer`.
    """

    def produce(out_queue: Queue[Dir | None]) -> None:
        async_diff_dir_walk_task(
            src=src, dst=dst, max_depth=max_depth, out_queue=out_queue, order=order
        )

    yield from iter_background_producer(produce, description="scan_missing_folders")
