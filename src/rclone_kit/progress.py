"""Progress-watching ergonomics shared between `JobHandle`
(`rclone_kit.job`) and `PartitionedJobHandle` (`rclone_kit.partitioned_job`).

Both types already expose `.done`/`.stats()` - the pull-based primitives
`_JobMonitor` backs. `watch()`/`on_progress()` add nothing to that
boundary: `watch()` is a plain generator sleeping between `stats()` calls,
`on_progress()` runs it on its own dedicated thread. Nothing here reaches
below the `_JobMonitor` boundary, and no native/ABI change is involved -
see "Job handles and the retry-aware copy endpoint" in
`docs/implementation_and_build_pipeline.md` for why progress is pulled
rather than pushed.

Defined against one narrow `ProgressSource` `Protocol` (matching this
codebase's existing narrow-Protocol pattern, `RcJobClient` in
`rc/jobs.py`) so the implementation is written once rather than
duplicated between `JobHandle` and `PartitionedJobHandle`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Protocol, Self

from rclone_kit.operation import TransferStats

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)

# Matches `job.py`'s module-level `_DEFAULT_POLL_INTERVAL_SECONDS`: a snapshot
# from `watch()`/`on_progress()` is never staler than the monitor's own
# cached status already is, and there is no reason to poll less often than
# the underlying cache can actually change.
_DEFAULT_WATCH_INTERVAL_SECONDS = 0.5


class ProgressSource(Protocol):
    """Narrow interface `watch()`/`on_progress()` depend on. `JobHandle`
    and `PartitionedJobHandle` both satisfy it structurally, with no
    explicit inheritance needed."""

    @property
    def done(self) -> bool: ...
    def stats(self) -> TransferStats: ...


def watch(
    source: ProgressSource, *, interval: float = _DEFAULT_WATCH_INTERVAL_SECONDS
) -> Iterator[TransferStats]:
    """Yield a `TransferStats` snapshot every `interval` seconds until
    `source` settles.

    A thin wrapper around `stats()`/`done` - encapsulates the sleep loop,
    nothing more. The final snapshot is always yielded last: the loop
    below only sleeps *between* not-yet-done snapshots, then yields one
    more, authoritative snapshot once `done` is observed true - `stats()`
    itself already guarantees that post-settle snapshot is the cached
    final one, not another in-flight RC call.
    """
    while not source.done:
        yield source.stats()
        time.sleep(interval)
    yield source.stats()


class ProgressSubscription:
    """A running `on_progress()` subscription.

    `stop()` and context-manager exit both end it early; either is safe to
    call after the subscription has already finished on its own (the
    underlying thread is simply already stopped, so `join()` returns
    immediately). Stopping can take up to one `interval` to take effect,
    since it is only checked between `watch()` snapshots - plus however
    long `source`'s own `stats()`/`done` take to return, since those are
    ordinary synchronous calls across the same native RC bridge every other
    blocking call in this codebase uses (`JobHandle.wait()` included), with
    no independent timeout of their own.
    """

    def __init__(self, thread: threading.Thread, stop_event: threading.Event) -> None:
        self._thread = thread
        self._stop_event = stop_event

    def stop(self, timeout: float | None = None) -> bool:
        """Request the subscription to stop and wait for its thread to
        exit.

        `timeout` bounds only this wait, matching `JobHandle.close()`'s
        bounded-wait convention - it never leaves the subscription
        "half stopped": the stop request itself (`stop_event`) is set
        unconditionally regardless of `timeout`, so the thread still stops
        as soon as it next checks, even if this call returns first.
        Returns `True` if the thread had actually exited by the time this
        call returned. Calling `stop()` from inside the subscription's own
        callback is safe and returns immediately without joining - a
        thread can never join itself.
        """
        self._stop_event.set()
        if threading.current_thread() is self._thread:
            return False
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()


def on_progress(
    source: ProgressSource,
    callback: Callable[[TransferStats], None],
    *,
    interval: float = _DEFAULT_WATCH_INTERVAL_SECONDS,
) -> ProgressSubscription:
    """Run `callback` with each `watch()` snapshot on a dedicated
    background thread, until `source` settles or the returned subscription
    is stopped.

    Never runs on `_JobMonitor`'s shared poll thread - each subscription
    gets its own thread, so one job's slow callback can never delay
    another job's status polling. A callback exception is logged and
    swallowed, never crashes the thread or propagates to the caller.
    """
    stop_event = threading.Event()

    def _run() -> None:
        for snapshot in watch(source, interval=interval):
            if stop_event.is_set():
                return
            try:
                callback(snapshot)
            except Exception:
                logger.exception("on_progress callback raised; continuing")

    thread = threading.Thread(target=_run, daemon=True, name="rclone-kit-on-progress")
    thread.start()
    return ProgressSubscription(thread, stop_event)
