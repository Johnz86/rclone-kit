"""Public `JobHandle` and the internal per-client `_JobMonitor` that backs
it (CLI-to-C-ABI migration Wave D design, sections 3/7/D3).

A `JobHandle` is a thin, thread-safe view over one `_JobRecord` owned by a
`_JobMonitor`. The monitor - not the handle - owns mutation: exactly one
background thread per embedded `Rclone` client polls every tracked,
not-yet-settled job through the `RcJobClient` boundary (`rc/jobs.py`),
caches the latest typed status/stats, and captures a job's terminal state
(as an `OperationResult`, or a terminal exception) before rclone's own
`job/status` expiry window can lose it. No user code runs on the monitor
thread; progress is pull-based through `JobHandle.status()`/`.stats()`.

`JobState.CANCELLED`/`JobState.LOST` never come out of `rc/jobs.py`'s
parser - only this module produces them, since only this module tracks
"did *this* client ask to cancel this job" and "did rclone's own record
disappear before we ever saw a terminal state".
"""

from __future__ import annotations

import contextlib
import dataclasses
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

from rclone_kit.exceptions import (
    JobExpiredError,
    OperationCancelledError,
    OperationFailedError,
    OperationTimeoutError,
)
from rclone_kit.operation import JobState, JobStatus, OperationResult, TransferStats
from rclone_kit.rc.jobs import RcJobNotFoundError, RcJobRef

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rclone_kit.rc.jobs import RcJobClient

_DEFAULT_POLL_INTERVAL_SECONDS = 0.5
_DEFAULT_CLOSE_WAIT_SECONDS = 5.0

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
    separately serializes the RC calls a poll makes, so a `cancel()`-
    triggered poll and the monitor thread's own poll of the same job can
    never both dispatch RC calls or both settle the record."""

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
        with self._record.condition:
            cached = self._record.latest_stats
        if cached is not None:
            return cached
        return self._monitor.stats_now(self._record)

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
    - not one thread per job.
    """

    def __init__(
        self,
        job_client: RcJobClient,
        *,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        close_wait_seconds: float = _DEFAULT_CLOSE_WAIT_SECONDS,
    ) -> None:
        self._job_client = job_client
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
        concurrent `cancel()` calls) never stops the same job twice."""
        with record.condition:
            if record.is_settled or record.cancel_requested:
                return
            record.cancel_requested = True
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
        try:
            stats = self._job_client.stats(record.ref.group)
        except Exception:
            stats = _EMPTY_STATS
        with record.condition:
            if record.latest_stats is None:
                record.latest_stats = stats
            stats = record.latest_stats
        return stats

    def shutdown(self, *, deadline_seconds: float) -> bool:
        """Cancel and wait for every job this monitor is tracking.

        Returns `True` if every job settled before the deadline. Stops and
        joins the polling thread only after that wait, so the background
        thread is still the one making progress while we wait.
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

        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        return all_settled

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
        for record in pending:
            self._poll_record(record)

    def _poll_record(self, record: _JobRecord) -> None:
        with record.poll_lock:
            if record.is_settled:
                return
            try:
                status = self._job_client.status(record.ref)
            except RcJobNotFoundError:
                self._settle_lost(record)
                return
            except Exception as error:
                with record.condition:
                    record.terminal_exception = error
                    record.condition.notify_all()
                self._forget(record)
                return

            if status.state.is_terminal:
                self._settle_terminal(record, status)
            else:
                with record.condition:
                    record.latest_status = status
                    record.condition.notify_all()

    def _settle_terminal(self, record: _JobRecord, status: JobStatus) -> None:
        try:
            final_stats = self._job_client.stats(record.ref.group)
        except Exception:
            final_stats = record.latest_stats or _EMPTY_STATS

        cancelled = record.cancel_requested and status.state is JobState.FAILED
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
            attempts=(),
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
            error="job record expired before its terminal state could be observed",
            output={},
        )
        error = JobExpiredError(record.ref.job_id, record.ref.execute_id)
        with record.condition:
            record.latest_status = lost_status
            record.terminal_exception = error
            record.condition.notify_all()
        self._forget(record)

    def _forget(self, record: _JobRecord) -> None:
        with self._records_lock:
            self._records.pop(record.ref.job_id, None)
