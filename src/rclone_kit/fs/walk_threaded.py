import contextlib
import warnings
from collections.abc import Generator
from queue import Empty, Queue
from threading import Event, Thread

from rclone_kit.fs.filesystem import FSPath
from rclone_kit.fs.walk_threaded_walker import FSWalker

_CLOSE_JOIN_TIMEOUT_SECONDS = 30.0
_DRAIN_POLL_SECONDS = 0.1


def os_walk_threaded_begin(self: FSPath, max_backlog: int = 8) -> FSWalker:
    return FSWalker(self, max_backlog)


class FSWalkThread:
    def __init__(self, fspath: FSPath, max_backlog: int = 8):
        self.fspath = fspath
        self.result_queue: Queue[tuple[FSPath, list[str], list[str]] | None] = Queue(
            maxsize=max_backlog
        )
        self.thread = Thread(target=self.worker, daemon=True)
        self.stop_event = Event()
        self._closed = False
        self.start()

    def worker(self):
        from rclone_kit.fs.walk import fs_walk

        for root, dirnames, filenames in fs_walk(self.fspath):
            if self.stop_event.is_set():
                break
            self.result_queue.put((root, dirnames, filenames))
        self.result_queue.put(None)

    def start(self):
        self.thread.start()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def get_results(self) -> Generator[tuple[FSPath, list[str], list[str]]]:
        while True:
            result = self.result_queue.get()
            if result is None:
                break
            yield result

    def close(self) -> None:
        """Stop the background walk and wait for it to finish.

        Idempotent, and safe to call whether or not a caller is using the
        context-manager protocol. While waiting, keeps draining
        `result_queue` so a worker currently blocked inside a full
        `put()` notices `stop_event` and exits instead of blocking
        forever once nothing else is consuming the queue.
        """
        if self._closed:
            return
        self._closed = True
        self.stop_event.set()
        while self.thread.is_alive():
            with contextlib.suppress(Empty):
                self.result_queue.get(timeout=_DRAIN_POLL_SECONDS)
        self.join(timeout=_CLOSE_JOIN_TIMEOUT_SECONDS)
        if self.thread.is_alive():
            warnings.warn(
                "FSWalkThread background walk did not finish within "
                f"{_CLOSE_JOIN_TIMEOUT_SECONDS}s of close()",
                stacklevel=2,
            )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
