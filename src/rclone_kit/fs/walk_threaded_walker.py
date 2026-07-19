from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rclone_kit.fs.filesystem import FSPath


@dataclass
class FSWalker:
    """Threaded"""

    fspath: FSPath
    max_backlog: int

    def __enter__(self):
        from rclone_kit.fs.walk_threaded import FSWalkThread

        self.walker = FSWalkThread(self.fspath, self.max_backlog)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        """Stop the background walk. A no-op if never entered via `with`."""
        walker = getattr(self, "walker", None)
        if walker is not None:
            walker.close()

    def __iter__(self):
        return self.walk()

    def walk(self):
        return self.walker.get_results()
