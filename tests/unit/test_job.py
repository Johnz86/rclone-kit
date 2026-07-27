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

import pytest
from fake_job_client import POLL_INTERVAL as _POLL_INTERVAL
from fake_job_client import WAIT_TIMEOUT as _WAIT_TIMEOUT
from fake_job_client import FakeJobClient
from fake_job_client import monitor as _monitor
from fake_job_client import status as _status
from fake_job_client import stub_stats as _stub_stats
from fake_job_client import wait_until as _wait_until

from rclone_kit.exceptions import (
    JobExpiredError,
    JobIdentityError,
    JobRuntimeClosedError,
    OperationCancelledError,
    OperationFailedError,
    OperationTimeoutError,
)
from rclone_kit.job import _IDENTITY_MISMATCH_STATUS_ERROR, _RUNTIME_CLOSED_STATUS_ERROR
from rclone_kit.native.errors import RuntimeClosedError
from rclone_kit.operation import JobState, TransferStats
from rclone_kit.rc.jobs import RcJobNotFoundError, RcJobRef


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

    def test_wait_result_carries_attempts_from_job_output(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "rclonekit/copy",
            {},
            group="g1",
            operation="copy",
            source="a",
            destination="b",
            check=True,
        )
        attempt_payload = {
            "number": 1,
            "startTime": "2026-07-23T20:24:38.3442603+02:00",
            "endTime": "2026-07-23T20:24:38.3862217+02:00",
            "duration": 0.04,
            "ok": True,
            "fatalError": False,
            "retryError": False,
        }
        job_client.queue_status(
            handle.job_id,
            _status(
                handle.job_id,
                handle.execute_id,
                "g1",
                state=JobState.SUCCEEDED,
                output={"attempts": [attempt_payload]},
            ),
        )

        result = handle.wait(timeout=_WAIT_TIMEOUT)

        assert len(result.attempts) == 1
        assert result.attempts[0].number == 1
        assert result.attempts[0].ok is True

    def test_wait_result_attempts_is_empty_when_output_has_no_attempts_key(self) -> None:
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

        assert result.attempts == ()

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

    def test_stats_refreshes_while_the_job_is_still_running(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        # job never settles (default RUNNING status), so every stats() call
        # below must hit the fake RC client fresh rather than freezing on
        # whichever snapshot happened to be cached first
        _stub_stats(job_client, "g1")
        first = handle.stats()
        assert first.bytes == 100

        job_client.set_stats(
            "g1",
            TransferStats(
                bytes=250,
                total_bytes=1000,
                checks=0,
                total_checks=0,
                transfers=1,
                total_transfers=4,
                errors=0,
                fatal_error=False,
                retry_error=False,
                speed=20.0,
                eta_seconds=None,
                elapsed_seconds=5.0,
            ),
        )
        second = handle.stats()

        assert second.bytes == 250
        assert not handle.done

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
        # dispatched on a background thread, not synchronously by cancel() itself
        _wait_until(lambda: len(job_client.stop_calls) == 1)

    def test_cancel_is_idempotent_before_terminal(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )

        assert handle.cancel() is True
        assert handle.cancel() is True  # already requested; still "accepted", but...
        _wait_until(lambda: len(job_client.stop_calls) >= 1)
        time.sleep(_POLL_INTERVAL * 2)  # let any wrongly-duplicated dispatch land
        assert len(job_client.stop_calls) == 1  # ...stop() itself is called only once

    def test_cancel_does_not_block_on_the_stop_rc_call(self) -> None:
        """`cancel()` is documented as never blocking - it must return before
        the `job/stop` RC call it dispatches even completes, not after."""
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        release = threading.Event()
        original_stop = job_client.stop

        def _slow_stop(ref: RcJobRef) -> None:
            release.wait(timeout=_WAIT_TIMEOUT)
            original_stop(ref)

        job_client.stop = _slow_stop  # type: ignore[method-assign]

        started = time.monotonic()
        accepted = handle.cancel()
        elapsed = time.monotonic() - started

        release.set()
        assert accepted is True
        assert elapsed < 0.1

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

    def test_an_unrelated_failure_after_a_cancel_request_is_not_misclassified(self) -> None:
        # A FAILED terminal state after cancel_requested=True is only a real
        # cancellation if rclone's own error text says so ("context
        # canceled"); an independent failure that merely races the cancel
        # request must stay FAILED.
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
                error="disk full",
            ),
        )

        handle.cancel()
        result = handle.wait(timeout=_WAIT_TIMEOUT)

        assert result.cancelled is False
        assert result.ok is False
        assert result.error == "disk full"
        assert handle.status().state is JobState.FAILED


class TestTransientPollingErrors:
    def test_a_transient_status_error_does_not_settle_the_job(self) -> None:
        # Only RcJobNotFoundError is authoritative for "the job is gone."
        # Any other exception from a status() call (network hiccup, a
        # parsing error) must not be mistaken for the job itself failing -
        # the record must stay tracked so the next poll can retry.
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(
            handle.job_id,
            RuntimeError("transient network error"),
            _status(handle.job_id, handle.execute_id, "g1", state=JobState.SUCCEEDED),
        )

        result = handle.wait(timeout=_WAIT_TIMEOUT)

        assert result.ok is True
        assert handle.status().state is JobState.SUCCEEDED

    def test_a_transient_status_error_leaves_the_record_tracked_for_retry(self) -> None:
        # The complement of the identity-mismatch case below: an error the
        # monitor cannot prove is permanent must keep the record tracked and
        # unsettled, however many polls in a row it fails.
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(handle.job_id, RuntimeError("transient network error"))

        time.sleep(_POLL_INTERVAL * 5)

        assert not handle.done
        assert handle.job_id in monitor._records


class TestJobIdentityMismatch:
    def test_identity_mismatch_settles_the_job_and_wait_reraises_it(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=True
        )
        job_client.queue_status(
            handle.job_id,
            JobIdentityError(handle.job_id, handle.execute_id, "exec-after-rclone-restart"),
        )

        with pytest.raises(JobIdentityError):
            handle.wait(timeout=_WAIT_TIMEOUT)

        assert handle.done
        assert handle.status().state is JobState.LOST
        assert handle.status().error == _IDENTITY_MISMATCH_STATUS_ERROR

    def test_identity_mismatch_forgets_the_record_and_stops_polling_it(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(
            handle.job_id,
            JobIdentityError(handle.job_id, handle.execute_id, "exec-after-rclone-restart"),
        )

        with pytest.raises(JobIdentityError):
            handle.wait(timeout=_WAIT_TIMEOUT)
        reads_at_settle = len(job_client.status_reads)
        time.sleep(_POLL_INTERVAL * 5)

        assert handle.job_id not in monitor._records
        assert len(job_client.status_reads) == reads_at_settle

    def test_shutdown_settles_a_job_that_failed_identity_validation(self) -> None:
        # Regression guard for `Rclone.close()`: an unsettled record makes
        # shutdown() burn its whole deadline and report False, leaving the
        # native runtime open.
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(
            handle.job_id,
            JobIdentityError(handle.job_id, handle.execute_id, "exec-after-rclone-restart"),
        )

        all_settled = monitor.shutdown(deadline_seconds=_WAIT_TIMEOUT)

        assert all_settled is True
        assert handle.done


class TestJobRuntimeClosed:
    """A runtime closed out from under a live job.

    `RcloneRuntime`'s closed flag is a one-way latch, so every later
    `job/status` raises `RuntimeClosedError` too. Classified as transient,
    the record never settled: `wait()` blocked forever, `close()` burned
    its whole deadline, and the monitor thread logged a traceback every
    poll interval for the rest of the process's life.
    """

    def test_a_closed_runtime_settles_the_job_and_wait_reraises_it(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=True
        )
        job_client.queue_status(handle.job_id, RuntimeClosedError())

        with pytest.raises(JobRuntimeClosedError):
            handle.wait(timeout=_WAIT_TIMEOUT)

        assert handle.done
        assert handle.status().state is JobState.LOST
        assert handle.status().error == _RUNTIME_CLOSED_STATUS_ERROR

    def test_a_closed_runtime_forgets_the_record_and_stops_polling_it(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(handle.job_id, RuntimeClosedError())

        with pytest.raises(JobRuntimeClosedError):
            handle.wait(timeout=_WAIT_TIMEOUT)
        reads_at_settle = len(job_client.status_reads)
        time.sleep(_POLL_INTERVAL * 5)

        assert handle.job_id not in monitor._records
        assert len(job_client.status_reads) == reads_at_settle

    def test_shutdown_settles_a_job_whose_runtime_was_closed(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.queue_status(handle.job_id, RuntimeClosedError())

        all_settled = monitor.shutdown(deadline_seconds=_WAIT_TIMEOUT)

        assert all_settled is True
        assert handle.done


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

    def test_failed_shutdown_leaves_the_polling_thread_running(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        # never queue a terminal status yet; this attempt times out

        all_settled = monitor.shutdown(deadline_seconds=0.1)

        assert all_settled is False
        monitor_thread = monitor._thread
        assert monitor_thread is not None
        assert monitor_thread.is_alive()

    def test_shutdown_retry_makes_progress_after_a_failed_attempt(self) -> None:
        # A failed shutdown() must not kill the polling thread - otherwise a
        # caller that retries later (e.g. `Rclone.close()` after catching
        # `OperationShutdownError`) has no way to ever observe the job settle.
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        _stub_stats(job_client, "g1")
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        # never queue a terminal status yet; this attempt times out

        first_attempt = monitor.shutdown(deadline_seconds=0.1)
        assert first_attempt is False

        # only now does the job become observable as settled - nothing but
        # the still-running background poller can ever pick this up, since a
        # retried request_cancel() is a no-op once cancel_requested is set
        job_client.queue_status(
            handle.job_id, _status(handle.job_id, handle.execute_id, "g1", state=JobState.SUCCEEDED)
        )

        second_attempt = monitor.shutdown(deadline_seconds=_WAIT_TIMEOUT)

        assert second_attempt is True
        assert handle.done

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


class TestWatchAndOnProgress:
    def test_watch_yields_snapshots_until_the_job_settles(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        handle = monitor.start_job(
            "sync/copy", {}, group="g1", operation="copy", source="a", destination="b", check=False
        )
        job_client.set_stats(
            "g1",
            TransferStats(
                bytes=10,
                total_bytes=100,
                checks=0,
                total_checks=0,
                transfers=0,
                total_transfers=1,
                errors=0,
                fatal_error=False,
                retry_error=False,
                speed=1.0,
                eta_seconds=None,
                elapsed_seconds=1.0,
            ),
        )

        seen: list[int] = []

        def _drive() -> None:
            for snapshot in handle.watch(interval=0.01):
                seen.append(snapshot.bytes)
                if len(seen) == 1:
                    job_client.set_stats(
                        "g1",
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
                            speed=1.0,
                            eta_seconds=None,
                            elapsed_seconds=1.0,
                        ),
                    )
                    job_client.queue_status(
                        handle.job_id,
                        _status(handle.job_id, handle.execute_id, "g1", state=JobState.SUCCEEDED),
                    )

        thread = threading.Thread(target=_drive)
        thread.start()
        thread.join(timeout=_WAIT_TIMEOUT)

        assert not thread.is_alive()
        assert seen[0] == 10
        assert seen[-1] == 100
        assert handle.done

    def test_watch_on_an_already_settled_job_yields_the_final_snapshot_once(self) -> None:
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

        snapshots = list(handle.watch(interval=0.01))

        assert len(snapshots) == 1
        assert snapshots[0].bytes == 100

    def test_on_progress_runs_on_a_dedicated_thread_and_reports_the_final_snapshot(self) -> None:
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
        received: list[TransferStats] = []
        thread_names: list[str] = []

        def _callback(snapshot: TransferStats) -> None:
            received.append(snapshot)
            thread_names.append(threading.current_thread().name)

        subscription = handle.on_progress(_callback, interval=0.01)
        _wait_until(lambda: len(received) >= 1)
        subscription.stop()

        assert received
        assert received[-1].bytes == 100
        assert all(name != threading.current_thread().name for name in thread_names)
        assert all(name != "rclone-kit-job-monitor" for name in thread_names)


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
