from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rclone_kit.fs.walk_threaded import FSWalkThread

if TYPE_CHECKING:
    from rclone_kit.fs.filesystem import FSPath

_NOT_ENTERED_MESSAGE = (
    "FSWalker must be used as a context manager, because it owns a "
    "background walk thread that only close() can stop:\n\n"
    "    with fspath.walk_begin() as walker:\n"
    "        for root, dirnames, filenames in walker:\n"
    "            ...\n"
)


@dataclass
class FSWalker:
    """Threaded walk of `fspath`, owned by a `with` block.

    `walker` is created on `__enter__` rather than on construction so the
    background thread starts and stops with a scope that is guaranteed to
    call `close()`. Using the object without `with` is therefore refused
    outright instead of being made to work lazily: `FSWalkThread`'s worker
    blocks on a full `result_queue` as soon as a consumer stops early, and
    only `close()` unblocks it, so a lazily started walker would silently
    strand a thread for the life of the process - exactly the leak
    `close()` exists to prevent. `walk()` therefore raises
    `_NOT_ENTERED_MESSAGE`, which names the missing `with` instead of
    pointing at this class's internals.
    """

    fspath: FSPath
    max_backlog: int
    walker: FSWalkThread | None = field(default=None, init=False)

    def __enter__(self):
        self.walker = FSWalkThread(self.fspath, self.max_backlog)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        """Stop the background walk. A no-op if never entered via `with`."""
        if self.walker is not None:
            self.walker.close()

    def __iter__(self):
        return self.walk()

    def walk(self):
        if self.walker is None:
            raise RuntimeError(_NOT_ENTERED_MESSAGE)
        return self.walker.get_results()
