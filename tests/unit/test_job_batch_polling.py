"""Unit tests for `_JobMonitor`'s batched polling: the round-trip budget it
buys, and the ways one bad job - or one bad batch - must not poison the
rest.

Batching is the reason `Rclone.start_copy_files()` can start hundreds of
partition jobs without the poll interval degrading linearly with partition
count, so the call-count assertions here are the point of the feature, not
incidental. Everything is asserted against fakes: `job/batch` is only
reachable through a built native library, which unit tests do not have, so
the fallback these tests pin down is what keeps an unverifiable transport
from ever stranding a job unsettled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fake_job_client import WAIT_TIMEOUT as _WAIT_TIMEOUT
from fake_job_client import FakeJobClient, UnbatchedJobClient
from fake_job_client import monitor as _monitor
from fake_job_client import status as _status
from fake_job_client import stub_stats as _stub_stats
from fake_job_client import wait_until as _wait_until

from rclone_kit.exceptions import JobIdentityError
from rclone_kit.operation import JobState
from rclone_kit.rc.errors import RcCallError
from rclone_kit.rc.jobs import _BATCH_METHOD, _STATUS_METHOD, RcJobNotFoundError

if TYPE_CHECKING:
    from rclone_kit.job import JobHandle, _JobMonitor

_POLLED_JOB_COUNT = 25
_OBSERVED_TICKS = 3
_FALLBACK_JOB_COUNT = 2
_BATCH_UNSUPPORTED_ERROR = RcCallError(
    _BATCH_METHOD, 404, {"error": f'couldn\'t find method "{_BATCH_METHOD}"'}
)


def _start_job(monitor: _JobMonitor, job_client: UnbatchedJobClient, group: str) -> JobHandle:
    _stub_stats(job_client, group)
    return monitor.start_job(
        "sync/copy", {}, group=group, operation="copy", source="a", destination="b", check=False
    )


def _start_jobs(
    monitor: _JobMonitor, job_client: UnbatchedJobClient, count: int
) -> list[JobHandle]:
    """Start `count` jobs that keep reporting RUNNING until `_finish()`."""
    return [_start_job(monitor, job_client, f"g{index}") for index in range(count)]


def _finish(job_client: UnbatchedJobClient, handles: list[JobHandle]) -> None:
    """Let every handle settle, so a test never leaves a poll thread
    spinning on jobs that can only ever time out."""
    for handle in handles:
        job_client.queue_status(
            handle.job_id,
            _status(handle.job_id, handle.execute_id, handle.group, state=JobState.SUCCEEDED),
        )


def test_batched_polling_costs_one_round_trip_per_tick_for_many_jobs() -> None:
    job_client = FakeJobClient()
    monitor = _monitor(job_client)
    handles = _start_jobs(monitor, job_client, _POLLED_JOB_COUNT)

    _wait_until(lambda: len(job_client.status_reads) >= _POLLED_JOB_COUNT * _OBSERVED_TICKS)
    round_trips = list(job_client.round_trips)
    status_reads = len(job_client.status_reads)

    assert set(round_trips) == {_BATCH_METHOD}
    assert len(round_trips) < status_reads
    assert len(round_trips) <= status_reads // _POLLED_JOB_COUNT + 1
    _finish(job_client, handles)
    assert monitor.shutdown(deadline_seconds=_WAIT_TIMEOUT) is True


def test_unbatched_polling_costs_one_round_trip_per_job_per_tick() -> None:
    """The baseline batching replaced, kept as a live comparison: without
    the batch capability the same job set costs one RC call per job per
    tick."""
    job_client = UnbatchedJobClient()
    monitor = _monitor(job_client)
    handles = _start_jobs(monitor, job_client, _POLLED_JOB_COUNT)

    _wait_until(lambda: len(job_client.status_reads) >= _POLLED_JOB_COUNT * _OBSERVED_TICKS)
    round_trips = list(job_client.round_trips)

    assert set(round_trips) == {_STATUS_METHOD}
    assert len(round_trips) >= _POLLED_JOB_COUNT * _OBSERVED_TICKS
    _finish(job_client, handles)
    assert monitor.shutdown(deadline_seconds=_WAIT_TIMEOUT) is True


def test_a_client_without_the_batch_capability_still_settles_its_jobs() -> None:
    job_client = UnbatchedJobClient()
    monitor = _monitor(job_client)
    handle = _start_job(monitor, job_client, "g1")
    _finish(job_client, [handle])

    result = handle.wait(timeout=_WAIT_TIMEOUT)

    assert result.ok
    assert set(job_client.round_trips) == {_STATUS_METHOD}


def test_a_failing_batch_call_falls_back_to_per_job_polling_and_jobs_settle() -> None:
    job_client = FakeJobClient()
    job_client.batch_error = _BATCH_UNSUPPORTED_ERROR
    monitor = _monitor(job_client)
    handles = _start_jobs(monitor, job_client, _FALLBACK_JOB_COUNT)
    _finish(job_client, handles)

    settled = monitor.shutdown(deadline_seconds=_WAIT_TIMEOUT)

    assert settled is True
    assert all(handle.wait(timeout=_WAIT_TIMEOUT).ok for handle in handles)
    assert _STATUS_METHOD in job_client.round_trips


def test_batching_is_abandoned_after_the_first_whole_call_failure() -> None:
    """A whole-call failure is structural, so retrying it every tick would
    only buy a doomed extra round-trip forever."""
    job_client = FakeJobClient()
    job_client.batch_error = _BATCH_UNSUPPORTED_ERROR
    monitor = _monitor(job_client)
    handles = _start_jobs(monitor, job_client, _FALLBACK_JOB_COUNT)

    _wait_until(
        lambda: (
            job_client.round_trips.count(_STATUS_METHOD) >= _FALLBACK_JOB_COUNT * _OBSERVED_TICKS
        )
    )

    assert job_client.round_trips.count(_BATCH_METHOD) == 1
    _finish(job_client, handles)
    assert monitor.shutdown(deadline_seconds=_WAIT_TIMEOUT) is True


def test_a_batch_answering_with_the_wrong_result_count_falls_back() -> None:
    """An index-misaligned response cannot be attributed to any job, so it
    must be applied to none of them - without killing the poll thread."""
    job_client = FakeJobClient()
    job_client.batch_results_override = []
    monitor = _monitor(job_client)
    handle = _start_job(monitor, job_client, "g1")
    _finish(job_client, [handle])

    result = handle.wait(timeout=_WAIT_TIMEOUT)

    assert result.ok
    assert job_client.round_trips.count(_BATCH_METHOD) == 1


def test_one_not_found_entry_settles_only_that_job_as_lost() -> None:
    job_client = FakeJobClient()
    monitor = _monitor(job_client)
    lost = _start_job(monitor, job_client, "g0")
    sibling = _start_job(monitor, job_client, "g1")
    job_client.queue_status(lost.job_id, RcJobNotFoundError(lost.job_id))
    _finish(job_client, [sibling])

    _wait_until(lambda: lost.done and sibling.done)

    assert lost.status().state is JobState.LOST
    assert sibling.wait(timeout=_WAIT_TIMEOUT).ok
    assert sibling.status().state is JobState.SUCCEEDED


def test_one_identity_mismatch_entry_settles_only_that_job() -> None:
    job_client = FakeJobClient()
    monitor = _monitor(job_client)
    mismatched = _start_job(monitor, job_client, "g0")
    sibling = _start_job(monitor, job_client, "g1")
    job_client.queue_status(
        mismatched.job_id,
        JobIdentityError(mismatched.job_id, mismatched.execute_id, "exec-after-rclone-restart"),
    )
    _finish(job_client, [sibling])

    _wait_until(lambda: mismatched.done and sibling.done)

    assert mismatched.status().state is JobState.LOST
    assert sibling.wait(timeout=_WAIT_TIMEOUT).ok


def test_one_transient_entry_error_leaves_only_that_record_tracked() -> None:
    job_client = FakeJobClient()
    monitor = _monitor(job_client)
    retried = _start_job(monitor, job_client, "g0")
    sibling = _start_job(monitor, job_client, "g1")
    job_client.queue_status(retried.job_id, RuntimeError("transient network error"))
    _finish(job_client, [sibling])

    assert sibling.wait(timeout=_WAIT_TIMEOUT).ok
    _wait_until(lambda: sibling.job_id not in monitor._records)

    assert not retried.done
    assert retried.job_id in monitor._records
