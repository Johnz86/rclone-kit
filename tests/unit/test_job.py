"""Unit tests for `rclone_kit.job`'s `JobHandle`/`_JobMonitor`.

Uses a fake `RcJobClient` and a short real poll interval (not a simulated
clock) - the monitor is genuine `threading.Thread` + `threading.Condition`
machinery, and a few hundredths of a second of real wall time keeps these
tests fast and deterministic in practice without the complexity of
injecting a virtual clock into live threads. Every wait in these tests
uses a generous timeout so they fail fast on a real regression rather than
hanging.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from rclone_kit.exceptions import (
    JobExpiredError,
    OperationCancelledError,
    OperationFailedError,
    OperationTimeoutError,
)
from rclone_kit.job import _JobMonitor
from rclone_kit.operation import JobState, JobStatus, TransferStats
from rclone_kit.rc.jobs import RcJobNotFoundError, RcJobRef

if TYPE_CHECKING:
    from collections.abc import Mapping

_POLL_INTERVAL = 0.02
_WAIT_TIMEOUT = 2.0
_NOW = datetime.now(UTC)


def _status(
    job_id: int, execute_id: str, group: str, *, state: JobState, **overrides: object
) -> JobStatus:
    fields: dict[str, object] = {
        "job_id": job_id,
        "execute_id": execute_id,
        "group": group,
        "state": state,
        "started_at": _NOW,
        "ended_at": _NOW + timedelta(seconds=1) if state.is_terminal else None,
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
            return _status(ref.job_id, ref.execute_id, ref.group, state=JobState.RUNNING)
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


def _monitor(job_client: FakeJobClient) -> _JobMonitor:
    return _JobMonitor(job_client, poll_interval_seconds=_POLL_INTERVAL, close_wait_seconds=2.0)


def _stub_stats(job_client: FakeJobClient, group: str) -> None:
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


class TestLazyMonitorStart:
    def test_no_thread_before_first_start_job(self) -> None:
        monitor = _monitor(FakeJobClient())
        assert not monitor.thread_started

    def test_thread_starts_on_first_start_job(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")

        monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=True
        )

        assert monitor.thread_started

    def test_one_thread_serves_many_jobs(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        handles = []
        for i in range(20):
            group = f"g{i}"
            _stub_stats(job_client, group)
            handle = monitor.start_job(
                "sync/copy",
                {},
                group=group,
                operation="copy",
                source="a",
                destination="b",
                check=True,
            )
            job_client.queue_status(
                handle.job_id,
                _status(handle.job_id, handle.execute_id, group, state=JobState.SUCCEEDED),
            )
            handles.append(handle)

        for handle in handles:
            result = handle.wait(timeout=_WAIT_TIMEOUT)
            assert result.ok

        first_thread = monitor._thread
        assert first_thread is not None
        # start_job() must not have spawned a second thread for the later jobs
        assert monitor._thread is first_thread
        monitor.shutdown(deadline_seconds=_WAIT_TIMEOUT)


class TestStartJob:
    def test_start_forwards_method_params_and_group(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")

        handle = monitor.start_job(
            "sync/copy",
            {"srcFs": "a", "dstFs": "b"},
            group="g1",
            operation="copy",
            source="a",
            destination="b",
            check=True,
        )

        assert job_client.starts == [("sync/copy", {"srcFs": "a", "dstFs": "b"}, "g1")]
        assert handle.group == "g1"


class TestWaitSuccessAndFailure:
    def test_wait_returns_ok_result_on_success(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=True
        )
        job_client.queue_status(
            handle.job_id, _status(handle.job_id, handle.execute_id, "g1", state=JobState.SUCCEEDED)
        )

        result = handle.wait(timeout=_WAIT_TIMEOUT)

        assert result.ok
        assert result.job_ids == (handle.job_id,)
        assert result.stats is not None
        assert result.stats.bytes == 100

    def test_wait_raises_operation_failed_error_when_check_true(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=True
        )
        job_client.queue_status(
            handle.job_id,
            _status(handle.job_id, handle.execute_id, "g1", state=JobState.FAILED, error="boom"),
        )

        with pytest.raises(OperationFailedError) as excinfo:
            handle.wait(timeout=_WAIT_TIMEOUT)
        assert excinfo.value.result.error == "boom"

    def test_wait_returns_failed_result_when_check_false(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(
            handle.job_id,
            _status(handle.job_id, handle.execute_id, "g1", state=JobState.FAILED, error="boom"),
        )

        result = handle.wait(timeout=_WAIT_TIMEOUT)

        assert result.ok is False
        assert result.error == "boom"

    def test_wait_timeout_raises_without_settling(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=True
        )
        # never queue a terminal status: job stays RUNNING forever

        with pytest.raises(OperationTimeoutError):
            handle.wait(timeout=0.1)
        assert not handle.done

    def test_multiple_waiters_all_receive_the_result(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=True
        )
        job_client.queue_status(
            handle.job_id, _status(handle.job_id, handle.execute_id, "g1", state=JobState.SUCCEEDED)
        )

        results: list[bool] = []
        errors: list[BaseException] = []

        def _wait() -> None:
            try:
                results.append(handle.wait(timeout=_WAIT_TIMEOUT).ok)
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=_wait) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_WAIT_TIMEOUT)

        assert not errors
        assert results == [True] * 5


class TestStatsAndStatusCaching:
    def test_stats_group_is_deleted_after_final_snapshot_cached(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=True
        )
        job_client.queue_status(
            handle.job_id, _status(handle.job_id, handle.execute_id, "g1", state=JobState.SUCCEEDED)
        )

        handle.wait(timeout=_WAIT_TIMEOUT)

        assert job_client.delete_stats_calls == ["g1"]

    def test_stats_after_completion_returns_cached_snapshot(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=True
        )
        job_client.queue_status(
            handle.job_id, _status(handle.job_id, handle.execute_id, "g1", state=JobState.SUCCEEDED)
        )
        handle.wait(timeout=_WAIT_TIMEOUT)

        calls_before = job_client._stats_calls_by_group.get("g1", 0)
        stats = handle.stats()

        assert stats.bytes == 100
        # cached snapshot: no additional core/stats call needed after settling
        assert job_client._stats_calls_by_group.get("g1", 0) == calls_before

    def test_status_reflects_running_then_succeeded(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=True
        )
        job_client.queue_status(
            handle.job_id,
            _status(handle.job_id, handle.execute_id, "g1", state=JobState.RUNNING),
            _status(handle.job_id, handle.execute_id, "g1", state=JobState.SUCCEEDED),
        )

        deadline = time.monotonic() + _WAIT_TIMEOUT
        while handle.status().state is not JobState.SUCCEEDED and time.monotonic() < deadline:
            time.sleep(0.01)

        assert handle.status().state is JobState.SUCCEEDED


class TestCancel:
    def test_cancel_before_terminal_calls_stop_and_returns_true(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )

        accepted = handle.cancel()

        assert accepted is True
        assert len(job_client.stop_calls) == 1

    def test_cancel_is_idempotent_before_terminal(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )

        assert handle.cancel() is True
        assert handle.cancel() is True  # already requested; still "accepted", but...
        assert len(job_client.stop_calls) == 1  # ...stop() itself is called only once

    def test_cancel_after_terminal_returns_false(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(
            handle.job_id, _status(handle.job_id, handle.execute_id, "g1", state=JobState.SUCCEEDED)
        )
        handle.wait(timeout=_WAIT_TIMEOUT)

        assert handle.cancel() is False
        assert job_client.stop_calls == []

    def test_cancel_reclassifies_a_subsequent_failed_status_as_cancelled(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(
            handle.job_id,
            _status(
                handle.job_id,
                handle.execute_id,
                "g1",
                state=JobState.FAILED,
                error="context canceled",
            ),
        )

        handle.cancel()
        result = handle.wait(timeout=_WAIT_TIMEOUT)

        assert result.cancelled is True
        assert result.ok is False
        assert handle.status().state is JobState.CANCELLED

    def test_check_true_raises_operation_cancelled_error(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=True
        )
        job_client.queue_status(
            handle.job_id,
            _status(
                handle.job_id,
                handle.execute_id,
                "g1",
                state=JobState.FAILED,
                error="context canceled",
            ),
        )

        handle.cancel()

        with pytest.raises(OperationCancelledError):
            handle.wait(timeout=_WAIT_TIMEOUT)

    def test_an_unrelated_failure_that_races_a_cancel_is_not_misclassified(self) -> None:
        # cancel_requested is only reclassified when the terminal state is
        # FAILED; a SUCCEEDED race must stay SUCCEEDED even if cancel() was
        # called moments before the job actually finished successfully.
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(
            handle.job_id, _status(handle.job_id, handle.execute_id, "g1", state=JobState.SUCCEEDED)
        )

        handle.cancel()
        result = handle.wait(timeout=_WAIT_TIMEOUT)

        assert result.cancelled is False
        assert result.ok is True


class TestJobExpiry:
    def test_job_not_found_before_any_terminal_observation_raises_job_expired_error(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=True
        )
        job_client.queue_status(handle.job_id, RcJobNotFoundError(handle.job_id))

        with pytest.raises(JobExpiredError):
            handle.wait(timeout=_WAIT_TIMEOUT)

    def test_lost_job_status_reports_lost_state(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(handle.job_id, RcJobNotFoundError(handle.job_id))

        with pytest.raises(JobExpiredError):
            handle.wait(timeout=_WAIT_TIMEOUT)
        assert handle.status().state is JobState.LOST
        assert handle.done


class TestShutdown:
    def test_shutdown_waits_for_active_jobs_to_settle(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(
            handle.job_id, _status(handle.job_id, handle.execute_id, "g1", state=JobState.SUCCEEDED)
        )

        all_settled = monitor.shutdown(deadline_seconds=_WAIT_TIMEOUT)

        assert all_settled is True
        assert handle.done

    def test_shutdown_cancels_still_running_jobs(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(
            handle.job_id,
            _status(
                handle.job_id,
                handle.execute_id,
                "g1",
                state=JobState.FAILED,
                error="context canceled",
            ),
        )

        monitor.shutdown(deadline_seconds=_WAIT_TIMEOUT)

        assert len(job_client.stop_calls) == 1
        assert handle.status().state is JobState.CANCELLED

    def test_shutdown_reports_false_when_a_job_never_settles(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        # never queue a terminal status; the fake keeps reporting RUNNING

        all_settled = monitor.shutdown(deadline_seconds=0.1)

        assert all_settled is False

    def test_shutdown_stops_the_polling_thread(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(
            handle.job_id, _status(handle.job_id, handle.execute_id, "g1", state=JobState.SUCCEEDED)
        )
        monitor_thread = monitor._thread
        assert monitor_thread is not None

        monitor.shutdown(deadline_seconds=_WAIT_TIMEOUT)

        assert not monitor_thread.is_alive()


class TestContextManager:
    def test_context_manager_closes_on_exit(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")

        with monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        ) as handle:
            job_client.queue_status(
                handle.job_id,
                _status(handle.job_id, handle.execute_id, "g1", state=JobState.SUCCEEDED),
            )

        assert handle.done
