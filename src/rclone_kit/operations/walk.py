import random
from collections.abc import Generator
from queue import Queue

from rclone_kit.background_producer import iter_background_producer
from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.remote import Remote
from rclone_kit.types import Order


def walk_runner_breadth_first(
    dir: Dir,
    max_depth: int,
    out_queue: Queue[DirListing | None],
    order: Order = Order.NORMAL,
) -> None:
    """Breadth-first counterpart to `walk_runner_depth_first`.

    Remaining depth is tracked per queued node (`(Dir, depth)` tuples)
    rather than by one counter shared across the traversal: every sibling
    at a level is entitled to the full remaining depth, and a single
    counter decremented once per dequeued node would exhaust after one
    node per level, leaving only the first branch walked past depth 1.

    Always puts a `None` sentinel before returning, success or failure -
    `walk()`'s consumer loop blocks on `out_queue.get()` forever
    otherwise. Any exception simply propagates to whoever called this
    function - a synchronous caller sees it directly; `walk()` runs this
    as a background thread's target and captures it there to re-raise
    from its own consumer loop.
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

    Descends with a single iterative stack, not recursive self-calls:
    exactly one sentinel may reach `out_queue`, and a recursive call
    would put its own on return - stopping the consumer at the end of the
    first subtree, with every later sibling's listing left unread in the
    queue.

    Each directory's listing is put onto `out_queue` before its
    subdirectories are pushed (pre-order), matching
    `walk_runner_breadth_first`'s ordering.

    Always puts a `None` sentinel before returning, success or failure -
    see `walk_runner_breadth_first`'s docstring for why, and for how a
    failure reaches the caller.
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


def walk(
    dir: Dir | Remote,
    breadth_first: bool,
    max_depth: int = -1,
    order: Order = Order.NORMAL,
) -> Generator[DirListing]:
    """Yield one `DirListing` per directory under `dir`, recursively
    (`max_depth=-1` for unlimited).

    `breadth_first` picks which runner traverses the tree; both put a
    directory's own listing before descending into it. The traversal runs
    on a background thread feeding a bounded queue, so listings arrive as
    they are produced and the whole tree is never held in memory at once
    - see `iter_background_producer` for that lifecycle.
    """
    root = Dir(dir) if isinstance(dir, Remote) else dir

    def produce(out_queue: Queue[DirListing | None]) -> None:
        if breadth_first:
            walk_runner_breadth_first(root, max_depth, out_queue, order)
        else:
            walk_runner_depth_first(root, max_depth, out_queue, order)

    yield from iter_background_producer(produce, description="walk")
