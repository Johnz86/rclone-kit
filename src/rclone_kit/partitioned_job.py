"""`PartitionedJobHandle`: a pure-Python aggregation of several `JobHandle`s
into one, for operations that partition a file list into multiple RC jobs
(`copy_files()`/`delete_files()` and their non-blocking `start_*`
counterparts in `operations/transfer_ops_embedded.py`).

Composes already-started `JobHandle`s - no RC calls or native changes of
its own. `.watch()`/`.on_progress()` delegate to `rclone_kit.progress`'s
module-level `watch()`/`on_progress()`, the same implementation
`JobHandle.watch()`/`.on_progress()` delegate to - written once against the
shared `progress.ProgressSource` Protocol (`.done`/`.stats()`), which this
class already satisfies structurally, with no inheritance needed.
"""

from __future__ import annotations

import contextlib
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

from rclone_kit.exceptions import OperationCancelledError, OperationFailedError
from rclone_kit.job import _DEFAULT_CLOSE_WAIT_SECONDS
from rclone_kit.operation import OperationResult, OperationWarning, TransferStats
from rclone_kit.progress import _DEFAULT_WATCH_INTERVAL_SECONDS, ProgressSubscription
from rclone_kit.progress import on_progress as _on_progress
from rclone_kit.progress import watch as _watch

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from rclone_kit.job import JobHandle


def _sum_stats(stats: Sequence[TransferStats]) -> TransferStats:
    """Combine every partition's stats snapshot into one.

    Only genuinely cumulative counters (bytes/checks/transfers/errors) are
    summed; `speed`/`eta_seconds`/`elapsed_seconds` describe a single job's
    own timeline and are not meaningfully summable across partitions that
    ran concurrently, so they are reported as `0.0`/`None` in the
    aggregate rather than inflated by partition count.
    """
    return TransferStats(
        bytes=sum(s.bytes for s in stats),
        total_bytes=sum(s.total_bytes for s in stats),
        checks=sum(s.checks for s in stats),
        total_checks=sum(s.total_checks for s in stats),
        transfers=sum(s.transfers for s in stats),
        total_transfers=sum(s.total_transfers for s in stats),
        errors=sum(s.errors for s in stats),
        fatal_error=any(s.fatal_error for s in stats),
        retry_error=any(s.retry_error for s in stats),
        speed=0.0,
        eta_seconds=None,
        elapsed_seconds=0.0,
    )


def aggregate_results(
    operation: str,
    source: str | None,
    destination: str | None,
    results: Sequence[OperationResult],
) -> OperationResult:
    """Fold every partition's `OperationResult` into a single composite one.

    An empty `results` (no partition needed to run - e.g. an empty input
    file list) yields a trivial `ok=True`, no-jobs-started result.
    """
    if not results:
        now = datetime.now(UTC)
        return OperationResult(
            ok=True,
            operation=operation,
            source=source,
            destination=destination,
            job_ids=(),
            stats=None,
            warnings=(),
            attempts=(),
            started_at=now,
            ended_at=now,
            duration=0.0,
            cancelled=False,
            error=None,
        )

    failures = [result for result in results if not result.ok]
    ok = not failures
    job_ids = tuple(job_id for result in results for job_id in result.job_ids)
    attempts = tuple(attempt for result in results for attempt in result.attempts)
    warnings = tuple(
        OperationWarning(
            message=f"{failure.source} -> {failure.destination}: {failure.error}",
            detail={
                "source": failure.source,
                "destination": failure.destination,
                "error": failure.error,
                "job_ids": failure.job_ids,
            },
        )
        for failure in failures
    )
    stats_list = [result.stats for result in results if result.stats is not None]
    stats = _sum_stats(stats_list) if stats_list else None
    started_at = min(result.started_at for result in results)
    ended_at = max(result.ended_at for result in results)
    cancelled = bool(failures) and all(failure.cancelled for failure in failures)
    error = (
        None
        if ok
        else "; ".join(
            f"{failure.source} -> {failure.destination}: {failure.error}" for failure in failures
        )
    )
    return OperationResult(
        ok=ok,
        operation=operation,
        source=source,
        destination=destination,
        job_ids=job_ids,
        stats=stats,
        warnings=warnings,
        attempts=attempts,
        started_at=started_at,
        ended_at=ended_at,
        duration=(ended_at - started_at).total_seconds(),
        cancelled=cancelled,
        error=error,
    )


def raise_if_check_failed(check: bool, aggregate: OperationResult) -> None:
    if not check or aggregate.ok:
        return
    if aggregate.cancelled:
        raise OperationCancelledError(aggregate)
    raise OperationFailedError(aggregate)


class PartitionedJobHandle:
    """A handle over several constituent `JobHandle`s started together as
    one logical partitioned operation.

    `.done`/`.stats()` aggregate every constituent handle - matching
    `ProgressSource`, so `progress.watch()`/`progress.on_progress()` work
    on this exactly as they do on a plain `JobHandle`. `.wait()` collects
    every handle's result (never aborting early on a partial failure, so a
    still-running sibling partition's result is never lost) before folding
    them into one `OperationResult` via `aggregate_results`, then raises
    once for the aggregate as a whole if `check` and `raise_if_check_failed`
    say so. Every constituent handle is started with its own `check=False`
    (see `operations/transfer_ops_embedded.py`'s partition-starting
    helpers) - only this aggregate-level `check` ever raises.

    `cleanup`, when given, runs once `.wait()` has collected every
    handle's result - never on a partial/timed-out `.wait()`, since a
    still-running sibling may still need whatever `cleanup` would remove
    (e.g. a partition's `--files-from` temp file). A caller that abandons
    a `PartitionedJobHandle` without ever calling `.wait()` to completion
    leaves `cleanup` unrun; `close()` makes a best-effort attempt to reach
    it by cancelling first.
    """

    def __init__(
        self,
        handles: Sequence[JobHandle],
        *,
        operation: str,
        source: str | None,
        destination: str | None,
        check: bool,
        cleanup: Callable[[], None] | None = None,
    ) -> None:
        self._handles = tuple(handles)
        self._operation = operation
        self._source = source
        self._destination = destination
        self._check = check
        self._cleanup = cleanup

    @property
    def done(self) -> bool:
        return all(handle.done for handle in self._handles)

    def stats(self) -> TransferStats:
        return _sum_stats([handle.stats() for handle in self._handles])

    def watch(
        self, *, interval: float = _DEFAULT_WATCH_INTERVAL_SECONDS
    ) -> Iterator[TransferStats]:
        """Yield an aggregated `TransferStats` snapshot every `interval`
        seconds until every constituent handle settles. See
        `rclone_kit.progress.watch`."""
        return _watch(self, interval=interval)

    def on_progress(
        self,
        callback: Callable[[TransferStats], None],
        *,
        interval: float = _DEFAULT_WATCH_INTERVAL_SECONDS,
    ) -> ProgressSubscription:
        """Run `callback` with each aggregated snapshot on a dedicated
        background thread until every constituent handle settles. See
        `rclone_kit.progress.on_progress`."""
        return _on_progress(self, callback, interval=interval)

    def wait(self, timeout: float | None = None) -> OperationResult:
        """Block until every constituent handle reaches a terminal state.

        `timeout` is a shared deadline budgeted across every handle (like
        `_JobMonitor.shutdown`'s own per-record budgeting), not a
        per-handle timeout - so one slow partition cannot silently consume
        the whole budget meant for the others. Raises whatever the first
        exhausted or failed handle raises; still-running siblings are left
        exactly as running, not cancelled.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        results: list[OperationResult] = []
        for handle in self._handles:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            results.append(handle.wait(timeout=remaining))

        aggregate = aggregate_results(self._operation, self._source, self._destination, results)
        if self._cleanup is not None:
            self._cleanup()
        raise_if_check_failed(self._check, aggregate)
        return aggregate

    def cancel(self) -> bool:
        """Request cancellation on every not-yet-settled constituent
        handle. Never blocks, matching `JobHandle.cancel()`. Returns
        `True` if at least one handle accepted the request.

        Every handle's `cancel()` is called unconditionally - not just
        until the first accepted request - since each has its own
        side-effecting cancel dispatch to make.
        """
        accepted = [handle.cancel() for handle in self._handles]
        return any(accepted)

    def close(self) -> None:
        """Cancel every unfinished constituent handle and wait up to
        `_DEFAULT_CLOSE_WAIT_SECONDS`, then attempt `cleanup`. Idempotent;
        does not raise on timeout, mirroring `JobHandle.close()`."""
        self.cancel()
        with contextlib.suppress(Exception):
            self.wait(timeout=_DEFAULT_CLOSE_WAIT_SECONDS)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
