"""Public `JobHandle` and the internal per-client `_JobMonitor` that backs
it.

A `JobHandle` is a thin, thread-safe view over one `_JobRecord` owned by a
`_JobMonitor`. The monitor - not the handle - owns mutation: exactly one
background thread per embedded `Rclone` client polls every tracked,
not-yet-settled job through the `RcJobClient` boundary (`rc/jobs.py`),
caches the latest typed status/stats, and captures a job's terminal state
(as an `OperationResult`, or a terminal exception) before rclone's own
`job/status` expiry window can lose it. No user code runs on the monitor
thread; progress is pull-based through `JobHandle.status()`/`.stats()`.

One tick of that thread costs one RC round-trip regardless of how many
jobs it tracks, because it polls them all through `job/batch`
(`RcBatchStatusClient`). A partitioned copy over a wide file set starts
one job per partition, so the per-job polling this replaced made the
effective poll interval - and with it every `watch()`/`on_progress()`
consumer - degrade linearly with partition count.

`JobState.CANCELLED`/`JobState.LOST` never come out of `rc/jobs.py`'s
parser - only this module produces them, since only this module tracks
"did *this* client ask to cancel this job" and "did this job's terminal
state become permanently unobservable" (rclone's record expired, or the
job id was reused by a restarted rclone).
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

from rclone_kit.exceptions import (
    JobExpiredError,
    JobIdentityError,
    JobRuntimeClosedError,
    OperationCancelledError,
    OperationError,
    OperationFailedError,
    OperationTimeoutError,
)
from rclone_kit.native.errors import RuntimeClosedError
from rclone_kit.operation import JobState, JobStatus, OperationResult, TransferStats
from rclone_kit.progress import _DEFAULT_WATCH_INTERVAL_SECONDS, ProgressSubscription
from rclone_kit.progress import on_progress as _on_progress
from rclone_kit.progress import watch as _watch
from rclone_kit.rc.jobs import (
    RcBatchStatusClient,
    RcJobNotFoundError,
    RcJobRef,
    parse_operation_attempts,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from rclone_kit.rc.jobs import RcJobClient, RcJobStatusResult

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 0.5
_DEFAULT_CLOSE_WAIT_SECONDS = 5.0
_CANCELLATION_ERROR_MARKER = "context canceled"
_EXPIRED_STATUS_ERROR = "job record expired before its terminal state could be observed"
_IDENTITY_MISMATCH_STATUS_ERROR = (
    "job id was reused by a restarted rclone before this job's terminal state could be observed"
)
_RUNTIME_CLOSED_STATUS_ERROR = (
    "the runtime polling this job was closed before its terminal state could be observed"
)

_EMPTY_STATS = TransferStats(
    bytes=0,
    total_bytes=0,
    checks=0,
    total_checks=0,
    transfers=0,
    total_transfers=0,
    errors=0,
    fatal_error=False,
    retry_error=False,
    speed=0.0,
    eta_seconds=None,
    elapsed_seconds=0.0,
)


@dataclasses.dataclass
class _JobRecord:
    """Mutable state for one job, owned by `_JobMonitor`. All mutation and
    all reads of mutable fields happen under `condition`'s lock; `poll_lock`
    separately serializes *acting on* a poll outcome, so a `cancel()`-
    triggered poll and the monitor thread's own poll of the same job can
    never both settle the record or both dispatch a settle's RC calls
    (`core/stats`, `core/stats-delete`).

    Fetching a status sits outside `poll_lock`, since the monitor fetches
    every tracked job's status in one batched call that no single record's
    lock can span. That is safe because the fetch is a pure read with no
    effect on rclone, and because every applier re-checks `is_settled`
    under `poll_lock` before acting: an outcome that raced a settle is
    discarded unapplied. Two racing non-terminal snapshots can still land
    out of order, which at worst rewinds an advisory field such as
    `duration` by less than one poll interval; settling - the one decision
    that must never be raced - stays strictly serialized.
    """

    ref: RcJobRef
    operation: str
    source: str | None
    destination: str | None
    condition: threading.Condition = dataclasses.field(default_factory=threading.Condition)
    poll_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    cancel_requested: bool = False
    latest_status: JobStatus | None = None
    latest_stats: TransferStats | None = None
    terminal_result: OperationResult | None = None
    terminal_exception: Exception | None = None

    @property
    def is_settled(self) -> bool:
        return self.terminal_result is not None or self.terminal_exception is not None


class JobHandle:
    """A handle to one started rclone RC job.

    Never exposes a PID, signals, stdout pipes, or a subprocess return
    code. `status()`/`stats()` return the monitor's latest cached
    snapshot - after the job settles, they keep returning that same final
    snapshot even though rclone itself may have since expired or deleted
    the underlying record.
    """

    def __init__(self, monitor: _JobMonitor, record: _JobRecord, *, check: bool) -> None:
        self._monitor = monitor
        self._record = record
        self._check = check

    @property
    def job_id(self) -> int:
        return self._record.ref.job_id

    @property
    def execute_id(self) -> str:
        return self._record.ref.execute_id

    @property
    def group(self) -> str:
        return self._record.ref.group

    @property
    def done(self) -> bool:
        with self._record.condition:
            return self._record.is_settled

    def status(self) -> JobStatus:
        with self._record.condition:
            cached = self._record.latest_status
        if cached is not None:
            return cached
        return self._monitor.poll_now(self._record)

    def stats(self) -> TransferStats:
        """Return the latest transfer stats.

        While the job is still running, this always makes a fresh
        `core/stats` call - a cached non-terminal snapshot would otherwise
        freeze progress reporting at whatever the first call happened to
        see. Once the job has settled, the final cached snapshot is
        returned without another RC call, since the underlying stats group
        is deleted at settle time.
        """
        with self._record.condition:
            if self._record.is_settled and self._record.latest_stats is not None:
                return self._record.latest_stats
        return self._monitor.stats_now(self._record)

    def watch(
        self, *, interval: float = _DEFAULT_WATCH_INTERVAL_SECONDS
    ) -> Iterator[TransferStats]:
        """Yield a `TransferStats` snapshot every `interval` seconds until
        the job settles. The final snapshot is always yielded last. A thin
        wrapper around `stats()`/`done` - encapsulates the sleep loop,
        nothing more; see `rclone_kit.progress.watch`."""
        return _watch(self, interval=interval)

    def on_progress(
        self,
        callback: Callable[[TransferStats], None],
        *,
        interval: float = _DEFAULT_WATCH_INTERVAL_SECONDS,
    ) -> ProgressSubscription:
        """Run `callback` on a dedicated background thread every `interval`
        seconds until the job settles. Never runs on `_JobMonitor`'s shared
        poll thread, so one job's slow callback can never delay another
        job's status polling. A callback exception is logged and
        swallowed, never crashes the thread. Returns a subscription whose
        `.stop()`/context-manager exit ends it early; see
        `rclone_kit.progress.on_progress`."""
        return _on_progress(self, callback, interval=interval)

    def wait(self, timeout: float | None = None) -> OperationResult:
        """Block until the job reaches a terminal state.

        `timeout` bounds observation only; it never cancels the
        already-dispatched operation. Raises `OperationTimeoutError` if the
        deadline elapses first - call `cancel()` explicitly for
        cancel-on-timeout behavior.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._record.condition:
            while not self._record.is_settled:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise OperationTimeoutError(self._record.operation, timeout)  # type: ignore[arg-type]
                self._record.condition.wait(timeout=remaining)
            terminal_exception = self._record.terminal_exception
            result = self._record.terminal_result

        if terminal_exception is not None:
            raise terminal_exception
        assert result is not None
        if self._check and not result.ok:
            if result.cancelled:
                raise OperationCancelledError(result)
            raise OperationFailedError(result)
        return result

    def cancel(self) -> bool:
        """Request cancellation. Idempotent: returns `False` if the job's
        terminal state was already observed, `True` if a cancel request
        was (already, or just now) accepted. Never blocks; use `wait()` for
        confirmed termination."""
        with self._record.condition:
            if self._record.is_settled:
                return False
        self._monitor.request_cancel(self._record)
        return True

    def close(self) -> None:
        """Cancel an unfinished owned job and wait up to the monitor's
        bounded close-wait interval. Idempotent; does not raise on
        timeout - an unresponsive job is the monitor's/client's shutdown
        concern, not a per-handle error."""
        self.cancel()
        with contextlib.suppress(Exception):
            self.wait(timeout=self._monitor.close_wait_seconds)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


class _JobMonitor:
    """One lazily-started background poller per embedded `Rclone` client.

    The thread is created on the first `start_job()` call, never at
    import or client construction, and serves every job the client starts
    - not one thread per job, and not one RC call per job per tick.
    """

    def __init__(
        self,
        job_client: RcJobClient,
        *,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        close_wait_seconds: float = _DEFAULT_CLOSE_WAIT_SECONDS,
    ) -> None:
        self._job_client = job_client
        self._batch_client: RcBatchStatusClient | None = (
            job_client if isinstance(job_client, RcBatchStatusClient) else None
        )
        self._poll_interval_seconds = poll_interval_seconds
        self.close_wait_seconds = close_wait_seconds
        self._records_lock = threading.Lock()
        self._records: dict[int, _JobRecord] = {}
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def thread_started(self) -> bool:
        return self._thread is not None

    def start_job(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        group: str,
        operation: str,
        source: str | None,
        destination: str | None,
        check: bool,
    ) -> JobHandle:
        ref = self._job_client.start(method, params, group)
        record = _JobRecord(ref=ref, operation=operation, source=source, destination=destination)
        with self._records_lock:
            self._records[ref.job_id] = record
        self._ensure_thread_started()
        return JobHandle(self, record, check=check)

    def request_cancel(self, record: _JobRecord) -> None:
        """Idempotent: only the call that actually flips
        `cancel_requested` dispatches `job/stop`, so a race between
        `JobHandle.cancel()` and `_JobMonitor.shutdown()` (or two
        concurrent `cancel()` calls) never stops the same job twice.

        Flipping the flag is synchronous (cheap, in-memory), but the
        `job/stop` RC call and its follow-up poll happen on a dedicated
        background thread - `JobHandle.cancel()` is documented as never
        blocking, so it must not wait on a network round-trip here. The
        monitor's own polling thread would eventually observe the
        cancellation regardless; dispatching `stop()` promptly just avoids
        waiting a full poll interval for it to be requested.
        """
        with record.condition:
            if record.is_settled or record.cancel_requested:
                return
            record.cancel_requested = True
        threading.Thread(
            target=self._dispatch_cancel,
            args=(record,),
            daemon=True,
            name="rclone-kit-job-cancel",
        ).start()

    def _dispatch_cancel(self, record: _JobRecord) -> None:
        with contextlib.suppress(Exception):
            # best-effort: the next poll reveals the real state either way
            self._job_client.stop(record.ref)
        self._poll_record(record)

    def poll_now(self, record: _JobRecord) -> JobStatus:
        self._poll_record(record)
        with record.condition:
            status = record.latest_status
        if status is None:
            raise RuntimeError(f"no status available for job {record.ref.job_id} after polling")
        return status

    def stats_now(self, record: _JobRecord) -> TransferStats:
        """Fetch a fresh stats snapshot, unless the job already settled.

        A settled job's stats group has already been deleted (see
        `_settle_terminal`), so once `latest_stats` holds the final
        snapshot it is returned as-is rather than attempting (and failing)
        another RC call.
        """
        with record.condition:
            if record.is_settled and record.latest_stats is not None:
                return record.latest_stats
        try:
            stats = self._job_client.stats(record.ref.group)
        except Exception:
            with record.condition:
                if record.latest_stats is not None:
                    return record.latest_stats
            stats = _EMPTY_STATS
        with record.condition:
            if not record.is_settled or record.latest_stats is None:
                record.latest_stats = stats
            return record.latest_stats

    def shutdown(self, *, deadline_seconds: float) -> bool:
        """Cancel and wait for every job this monitor is tracking.

        Returns `True` if every job settled before the deadline AND the
        polling thread itself stopped. Stops and joins the polling thread
        only once every job has actually settled, so a failed shutdown
        (this method returning `False`) leaves the thread running - a
        caller that retries `shutdown()` later still has a live poller
        making progress on the still-unsettled jobs, rather than a
        permanently stopped thread that can never confirm them settled.
        """
        with self._records_lock:
            records = list(self._records.values())
        for record in records:
            self.request_cancel(record)

        end = time.monotonic() + deadline_seconds
        all_settled = True
        for record in records:
            remaining = max(0.0, end - time.monotonic())
            with record.condition:
                settled = record.condition.wait_for(
                    lambda r=record: r.is_settled, timeout=remaining
                )
            if not settled:
                all_settled = False

        if not all_settled:
            return False

        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
            if thread.is_alive():
                return False
        return True

    def _ensure_thread_started(self) -> None:
        with self._records_lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, daemon=True, name="rclone-kit-job-monitor"
                )
                self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._poll_once()
            self._stop_event.wait(self._poll_interval_seconds)

    def _poll_once(self) -> None:
        with self._records_lock:
            pending = [r for r in self._records.values() if not r.is_settled]
        if not pending:
            return
        polled = self._batched_statuses(pending)
        if polled is None:
            for record in pending:
                self._poll_record(record)
            return
        for record, outcome in polled:
            self._apply_poll_outcome(record, outcome)

    def _batched_statuses(
        self, records: Sequence[_JobRecord]
    ) -> list[tuple[_JobRecord, RcJobStatusResult]] | None:
        """Poll every record in one `job/batch` round-trip, or return `None`
        when this monitor has no usable batch transport and the caller must
        poll each record individually.

        Batching is disabled permanently on the first whole-call failure.
        The realistic causes are structural - a native build predating
        `job/batch`, or a response this client cannot read - so retrying
        every tick would only spend a doomed extra round-trip forever,
        whereas the per-job path it falls back to is exactly what this
        monitor did before batching existed and settles jobs just as
        reliably. Only the monitor thread ever reads or writes
        `_batch_client`, so this needs no lock.
        """
        batch_client = self._batch_client
        if batch_client is None:
            return None
        try:
            outcomes = batch_client.statuses([record.ref for record in records])
        except Exception:
            logger.warning(
                "batched job polling failed; falling back to one status call per job",
                exc_info=True,
            )
            self._batch_client = None
            return None
        if len(outcomes) != len(records):
            logger.warning(
                "batched job polling returned %d outcomes for %d jobs; "
                "falling back to one status call per job",
                len(outcomes),
                len(records),
            )
            self._batch_client = None
            return None
        return list(zip(records, outcomes, strict=True))

    def _poll_record(self, record: _JobRecord) -> None:
        """Poll exactly one record through its own `job/status` call.

        Used by `poll_now()` and by `cancel()`'s follow-up poll - a single
        record is never worth a `job/batch` envelope - and as the fallback
        `_poll_once` degrades to when batching is unavailable.
        """
        with record.condition:
            if record.is_settled:
                return
        self._apply_poll_outcome(record, self._fetch_status(record.ref))

    def _fetch_status(self, ref: RcJobRef) -> RcJobStatusResult:
        try:
            return self._job_client.status(ref)
        except Exception as error:
            return error

    def _apply_poll_outcome(self, record: _JobRecord, outcome: RcJobStatusResult) -> None:
        """Settle or refresh `record` from one poll's outcome, under
        `poll_lock` so exactly one poller ever makes that decision."""
        with record.poll_lock:
            if record.is_settled:
                return
            match outcome:
                case RcJobNotFoundError():
                    self._settle_lost(record)
                case JobIdentityError() as error:
                    self._settle_identity_mismatch(record, error)
                case RuntimeClosedError():
                    self._settle_runtime_closed(record)
                case Exception():
                    # Transient: a status-call/parsing error does not mean the
                    # job itself failed or disappeared - only `RcJobNotFoundError`
                    # is authoritative for that. Leave the record unsettled and
                    # still tracked so the next scheduled poll retries; settling
                    # here would falsely report a still-running job as failed.
                    logger.warning(
                        "transient error polling job %s; will retry",
                        record.ref.job_id,
                        exc_info=outcome,
                    )
                case status if status.state.is_terminal:
                    self._settle_terminal(record, status)
                case status:
                    with record.condition:
                        record.latest_status = status
                        record.condition.notify_all()

    def _settle_terminal(self, record: _JobRecord, status: JobStatus) -> None:
        try:
            final_stats = self._job_client.stats(record.ref.group)
        except Exception:
            final_stats = record.latest_stats or _EMPTY_STATS

        cancelled = (
            record.cancel_requested
            and status.state is JobState.FAILED
            and status.error is not None
            and _CANCELLATION_ERROR_MARKER in status.error
        )
        if cancelled:
            status = dataclasses.replace(status, state=JobState.CANCELLED)
        ok = status.state is JobState.SUCCEEDED
        error = status.error if not ok else None
        if not ok and error is None and not cancelled:
            error = "operation failed with no error message reported by rclone"

        assert status.ended_at is not None  # guaranteed by JobStatus's own terminal invariant
        result = OperationResult(
            ok=ok,
            operation=record.operation,
            source=record.source,
            destination=record.destination,
            job_ids=(record.ref.job_id,),
            stats=final_stats,
            warnings=(),
            attempts=parse_operation_attempts(status.output),
            started_at=status.started_at,
            ended_at=status.ended_at,
            duration=status.duration,
            cancelled=cancelled,
            error=error,
        )

        with contextlib.suppress(Exception):
            # stats-group cleanup is best-effort; the final snapshot is
            # already cached above regardless of whether this succeeds
            self._job_client.delete_stats(record.ref.group)

        with record.condition:
            record.latest_status = status
            record.latest_stats = final_stats
            record.terminal_result = result
            record.condition.notify_all()
        self._forget(record)

    def _settle_lost(self, record: _JobRecord) -> None:
        """Rclone's own job record is gone, so its terminal state expired
        before this client could observe it."""
        self._settle_unobservable(
            record,
            JobExpiredError(record.ref.job_id, record.ref.execute_id),
            status_error=_EXPIRED_STATUS_ERROR,
        )

    def _settle_identity_mismatch(self, record: _JobRecord, error: JobIdentityError) -> None:
        """A mismatched `execute_id` is a permanent identity fault, not a
        transient poll failure: rclone restarted, job ids restarted from
        one, and this job id now belongs to an unrelated job. Retrying
        could only ever re-observe that unrelated job, so the record is
        settled here instead of being left tracked forever - an unsettled
        record blocks `wait()` indefinitely and burns the whole
        `Rclone.close()` shutdown deadline."""
        self._settle_unobservable(record, error, status_error=_IDENTITY_MISMATCH_STATUS_ERROR)

    def _settle_runtime_closed(self, record: _JobRecord) -> None:
        """A closed runtime is permanent, not a transient poll failure.

        `RcloneRuntime._closed` is a one-way latch, so every subsequent
        `job/status` call raises `RuntimeClosedError` too. Left in the
        transient branch, the record would never settle: `wait()` would
        block forever, `Rclone.close()` would burn its whole shutdown
        deadline and then raise `OperationShutdownError`, and the monitor
        thread would log a fresh traceback every poll interval for the
        remaining life of the process.

        Reachable whenever a runtime is closed out from under live jobs -
        most plainly through `shared_runtime()`, which `production_usage.md`
        documents for exactly the multi-client case where the closer is not
        the client whose jobs are still outstanding.
        """
        self._settle_unobservable(
            record,
            JobRuntimeClosedError(record.ref.job_id),
            status_error=_RUNTIME_CLOSED_STATUS_ERROR,
        )

    def _settle_unobservable(
        self, record: _JobRecord, error: OperationError, *, status_error: str
    ) -> None:
        """Settle `record` terminally when this job's real outcome can
        never be observed, reporting `JobState.LOST`.

        `LOST` is documented in terms of rclone's expiry window, which
        describes an expired record exactly and an identity mismatch only
        by analogy. A dedicated enum member would still be the worse
        trade: `JobState` is public API, so every consumer matching
        exhaustively on it would break, while the two causes are the same
        fact to a caller - this handle's job is terminal and its outcome
        is unknowable. Which cause it was stays available in
        `JobStatus.error` and, authoritatively, in the `OperationError`
        subclass `wait()` re-raises.
        """
        now = datetime.now(UTC)
        with record.condition:
            started_at = record.latest_status.started_at if record.latest_status else now
        lost_status = JobStatus(
            job_id=record.ref.job_id,
            execute_id=record.ref.execute_id,
            group=record.ref.group,
            state=JobState.LOST,
            started_at=started_at,
            ended_at=now,
            duration=(now - started_at).total_seconds(),
            error=status_error,
            output={},
        )
        with record.condition:
            record.latest_status = lost_status
            record.terminal_exception = error
            record.condition.notify_all()
        self._forget(record)

    def _forget(self, record: _JobRecord) -> None:
        with self._records_lock:
            self._records.pop(record.ref.job_id, None)
