"""Unit tests for `rclone_kit.partitioned_job.PartitionedJobHandle`.

Built on the same fake `RcJobClient` + real `_JobMonitor` machinery
`test_job.py` uses (`fake_job_client`), since `PartitionedJobHandle` wraps
genuine `JobHandle`s, not a further fake of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fake_job_client import WAIT_TIMEOUT as _WAIT_TIMEOUT
from fake_job_client import FakeJobClient
from fake_job_client import monitor as _monitor
from fake_job_client import status as _status
from fake_job_client import stub_stats as _stub_stats

from rclone_kit.exceptions import (
    OperationCancelledError,
    OperationFailedError,
    OperationTimeoutError,
)
from rclone_kit.operation import JobState
from rclone_kit.partitioned_job import PartitionedJobHandle

if TYPE_CHECKING:
    from rclone_kit.job import JobHandle, _JobMonitor


def _start(monitor: _JobMonitor, job_client: FakeJobClient, group: str) -> JobHandle:
    _stub_stats(job_client, group)
    return monitor.start_job(
        "rclonekit/copy",
        {},
        group=group,
        operation="copy_files",
        source=f"src/{group}",
        destination=f"dst/{group}",
        check=False,
    )


class TestEmpty:
    def test_done_is_true_with_no_handles(self) -> None:
        handle = PartitionedJobHandle(
            (), operation="copy_files", source="a", destination="b", check=True
        )

        assert handle.done is True

    def test_stats_is_all_zero_with_no_handles(self) -> None:
        handle = PartitionedJobHandle(
            (), operation="copy_files", source="a", destination="b", check=True
        )

        stats = handle.stats()

        assert stats.bytes == 0
        assert stats.total_bytes == 0

    def test_wait_returns_a_trivial_ok_result_with_no_handles(self) -> None:
        handle = PartitionedJobHandle(
            (), operation="copy_files", source="a", destination="b", check=True
        )

        result = handle.wait()

        assert result.ok is True
        assert result.job_ids == ()


class TestDoneAndStats:
    def test_done_is_false_until_every_handle_settles(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        h1 = _start(monitor, job_client, "g1")
        h2 = _start(monitor, job_client, "g2")
        partitioned = PartitionedJobHandle(
            (h1, h2), operation="copy_files", source="a", destination="b", check=False
        )

        assert partitioned.done is False

        job_client.queue_status(
            h1.job_id, _status(h1.job_id, h1.execute_id, "g1", state=JobState.SUCCEEDED)
        )
        h1.wait(timeout=_WAIT_TIMEOUT)

        assert partitioned.done is False  # h2 still running

        job_client.queue_status(
            h2.job_id, _status(h2.job_id, h2.execute_id, "g2", state=JobState.SUCCEEDED)
        )
        h2.wait(timeout=_WAIT_TIMEOUT)

        assert partitioned.done is True

    def test_stats_sums_cumulative_counters_across_handles(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        h1 = _start(monitor, job_client, "g1")  # stub_stats -> bytes=100
        h2 = _start(monitor, job_client, "g2")  # stub_stats -> bytes=100
        partitioned = PartitionedJobHandle(
            (h1, h2), operation="copy_files", source="a", destination="b", check=False
        )

        stats = partitioned.stats()

        assert stats.bytes == 200
        assert stats.total_bytes == 200
        assert stats.transfers == 2


class TestWatch:
    def test_watch_yields_an_aggregated_snapshot_and_settles_once_every_handle_does(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        h1 = _start(monitor, job_client, "g1")
        h2 = _start(monitor, job_client, "g2")
        job_client.queue_status(
            h1.job_id, _status(h1.job_id, h1.execute_id, "g1", state=JobState.SUCCEEDED)
        )
        job_client.queue_status(
            h2.job_id, _status(h2.job_id, h2.execute_id, "g2", state=JobState.SUCCEEDED)
        )
        h1.wait(timeout=_WAIT_TIMEOUT)
        h2.wait(timeout=_WAIT_TIMEOUT)
        partitioned = PartitionedJobHandle(
            (h1, h2), operation="copy_files", source="a", destination="b", check=False
        )

        snapshots = list(partitioned.watch(interval=0.01))

        assert snapshots[-1].bytes == 200


class TestWait:
    def test_wait_aggregates_every_handle_and_never_aborts_on_partial_failure(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        h1 = _start(monitor, job_client, "g1")
        h2 = _start(monitor, job_client, "g2")
        job_client.queue_status(
            h1.job_id,
            _status(h1.job_id, h1.execute_id, "g1", state=JobState.FAILED, error="boom"),
        )
        job_client.queue_status(
            h2.job_id, _status(h2.job_id, h2.execute_id, "g2", state=JobState.SUCCEEDED)
        )
        partitioned = PartitionedJobHandle(
            (h1, h2), operation="copy_files", source="a", destination="b", check=False
        )

        result = partitioned.wait(timeout=_WAIT_TIMEOUT)

        assert result.ok is False
        assert set(result.job_ids) == {h1.job_id, h2.job_id}
        assert result.stats is not None
        assert result.stats.bytes == 200  # both partitions' stats still summed in

    def test_wait_raises_operation_failed_error_when_check_true(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        h1 = _start(monitor, job_client, "g1")
        job_client.queue_status(
            h1.job_id,
            _status(h1.job_id, h1.execute_id, "g1", state=JobState.FAILED, error="boom"),
        )
        partitioned = PartitionedJobHandle(
            (h1,), operation="copy_files", source="a", destination="b", check=True
        )

        with pytest.raises(OperationFailedError) as excinfo:
            partitioned.wait(timeout=_WAIT_TIMEOUT)
        assert excinfo.value.result.error is not None

    def test_wait_raises_operation_cancelled_error_for_a_cancelled_aggregate(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        h1 = _start(monitor, job_client, "g1")
        job_client.queue_status(
            h1.job_id,
            _status(
                h1.job_id, h1.execute_id, "g1", state=JobState.FAILED, error="context canceled"
            ),
        )
        partitioned = PartitionedJobHandle(
            (h1,), operation="copy_files", source="a", destination="b", check=True
        )
        h1.cancel()

        with pytest.raises(OperationCancelledError):
            partitioned.wait(timeout=_WAIT_TIMEOUT)

    def test_wait_runs_cleanup_after_every_handle_settles(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        h1 = _start(monitor, job_client, "g1")
        job_client.queue_status(
            h1.job_id, _status(h1.job_id, h1.execute_id, "g1", state=JobState.SUCCEEDED)
        )
        cleanup_calls: list[bool] = []
        partitioned = PartitionedJobHandle(
            (h1,),
            operation="copy_files",
            source="a",
            destination="b",
            check=False,
            cleanup=lambda: cleanup_calls.append(True),
        )

        partitioned.wait(timeout=_WAIT_TIMEOUT)

        assert cleanup_calls == [True]

    def test_wait_does_not_run_cleanup_on_timeout(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        h1 = _start(monitor, job_client, "g1")
        # never queue a terminal status: h1 stays RUNNING forever
        cleanup_calls: list[bool] = []
        partitioned = PartitionedJobHandle(
            (h1,),
            operation="copy_files",
            source="a",
            destination="b",
            check=False,
            cleanup=lambda: cleanup_calls.append(True),
        )

        with pytest.raises(OperationTimeoutError):
            partitioned.wait(timeout=0.05)

        assert cleanup_calls == []


class TestCancel:
    def test_cancel_requests_cancellation_on_every_handle(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        h1 = _start(monitor, job_client, "g1")
        h2 = _start(monitor, job_client, "g2")
        partitioned = PartitionedJobHandle(
            (h1, h2), operation="copy_files", source="a", destination="b", check=False
        )

        accepted = partitioned.cancel()

        assert accepted is True

    def test_cancel_after_every_handle_settled_returns_false(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        h1 = _start(monitor, job_client, "g1")
        job_client.queue_status(
            h1.job_id, _status(h1.job_id, h1.execute_id, "g1", state=JobState.SUCCEEDED)
        )
        h1.wait(timeout=_WAIT_TIMEOUT)
        partitioned = PartitionedJobHandle(
            (h1,), operation="copy_files", source="a", destination="b", check=False
        )

        assert partitioned.cancel() is False


class TestCloseAndContextManager:
    def test_close_cancels_and_waits(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        h1 = _start(monitor, job_client, "g1")
        job_client.queue_status(
            h1.job_id,
            _status(
                h1.job_id, h1.execute_id, "g1", state=JobState.FAILED, error="context canceled"
            ),
        )
        partitioned = PartitionedJobHandle(
            (h1,), operation="copy_files", source="a", destination="b", check=False
        )

        partitioned.close()  # must not raise

        assert h1.done

    def test_context_manager_closes_on_exit(self) -> None:
        job_client = FakeJobClient()
        monitor = _monitor(job_client)
        h1 = _start(monitor, job_client, "g1")

        with PartitionedJobHandle(
            (h1,), operation="copy_files", source="a", destination="b", check=False
        ):
            job_client.queue_status(
                h1.job_id, _status(h1.job_id, h1.execute_id, "g1", state=JobState.SUCCEEDED)
            )

        assert h1.done
