"""Shared `RcJobClient` test double for `test_job.py`/`test_partitioned_job.py`.

Real `_JobMonitor` + `threading.Thread`/`threading.Condition` machinery
against a fake RC boundary, not a simulated clock - a short real poll
interval keeps these tests fast and deterministic in practice without the
complexity of injecting a virtual clock into live threads.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from rclone_kit.job import _JobMonitor
from rclone_kit.operation import JobState, JobStatus, TransferStats
from rclone_kit.rc.jobs import RcJobRef

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

POLL_INTERVAL = 0.02
WAIT_TIMEOUT = 2.0
NOW = datetime.now(UTC)


def status(
    job_id: int, execute_id: str, group: str, *, state: JobState, **overrides: object
) -> JobStatus:
    fields: dict[str, object] = {
        "job_id": job_id,
        "execute_id": execute_id,
        "group": group,
        "state": state,
        "started_at": NOW,
        "ended_at": NOW + timedelta(seconds=1) if state.is_terminal else None,
        "duration": 1.0 if state.is_terminal else 0.0,
        "error": None,
        "output": {},
    }
    fields.update(overrides)
    return JobStatus(**fields)  # type: ignore[arg-type]


class FakeJobClient:
    """A fake `RcJobClient`. `status()` returns a queued sequence of
    `JobStatus`/exception entries per job ID, repeating the last queued
    entry; with nothing queued it returns a synthetic RUNNING status, so a
    test can start a job and queue its terminal status afterward without
    racing the monitor thread's first poll.
    """

    def __init__(self) -> None:
        self.starts: list[tuple[str, dict, str]] = []
        self.stop_calls: list[RcJobRef] = []
        self.delete_stats_calls: list[str] = []
        self._next_job_id = 1
        self._queues: dict[int, list[JobStatus | Exception]] = {}
        self._stats_by_group: dict[str, TransferStats] = {}
        self._stats_calls_by_group: dict[str, int] = {}

    def start(self, method: str, params: Mapping[str, object], group: str) -> RcJobRef:
        job_id = self._next_job_id
        self._next_job_id += 1
        self.starts.append((method, dict(params), group))
        return RcJobRef(job_id=job_id, execute_id=f"exec-{job_id}", group=group)

    def queue_status(self, job_id: int, *entries: JobStatus | Exception) -> None:
        self._queues[job_id] = list(entries)

    def status(self, ref: RcJobRef) -> JobStatus:
        queue = self._queues.get(ref.job_id)
        if not queue:
            return status(ref.job_id, ref.execute_id, ref.group, state=JobState.RUNNING)
        entry = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry

    def set_stats(self, group: str, stats: TransferStats) -> None:
        self._stats_by_group[group] = stats

    def stats(self, group: str) -> TransferStats:
        self._stats_calls_by_group[group] = self._stats_calls_by_group.get(group, 0) + 1
        stats = self._stats_by_group.get(group)
        if stats is None:
            raise AssertionError(f"no stats queued for group {group!r}")
        return stats

    def stop(self, ref: RcJobRef) -> None:
        self.stop_calls.append(ref)

    def delete_stats(self, group: str) -> None:
        self.delete_stats_calls.append(group)


def monitor(job_client: FakeJobClient) -> _JobMonitor:
    return _JobMonitor(job_client, poll_interval_seconds=POLL_INTERVAL, close_wait_seconds=2.0)


def wait_until(predicate: Callable[[], bool], timeout: float = WAIT_TIMEOUT) -> None:
    """Poll `predicate` until it's true, since e.g. `cancel()` dispatches
    its RC calls on a background thread and must not be observed
    synchronously."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def stub_stats(job_client: FakeJobClient, group: str) -> None:
    job_client.set_stats(
        group,
        TransferStats(
            bytes=100,
            total_bytes=100,
            checks=0,
            total_checks=0,
            transfers=1,
            total_transfers=1,
            errors=0,
            fatal_error=False,
            retry_error=False,
            speed=10.0,
            eta_seconds=None,
            elapsed_seconds=1.0,
        ),
    )
