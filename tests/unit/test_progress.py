"""Unit tests for `rclone_kit.progress`'s `watch()`/`on_progress()`.

Uses small fake `ProgressSource`s (not a real `JobHandle`/`_JobMonitor`)
since these functions are defined purely against the narrow `ProgressSource`
Protocol - `test_job.py`/`test_partitioned_job.py` separately confirm that
`JobHandle`/`PartitionedJobHandle` satisfy it correctly.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from rclone_kit.operation import TransferStats
from rclone_kit.progress import ProgressSubscription, on_progress, watch

_WAIT_TIMEOUT = 2.0


def _stats(bytes_done: int) -> TransferStats:
    return TransferStats(
        bytes=bytes_done,
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
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = _WAIT_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


class FakeProgressSource:
    """A `ProgressSource` whose `done` flips true once its queued snapshot
    sequence is exhausted; each `stats()` call consumes the next queued
    snapshot (repeating the last one once exhausted, matching a real
    settled handle's own "keep returning the final snapshot" contract).
    `done` is independent of `stats()` being called, matching a real
    `JobHandle` where the background monitor - not a `stats()` caller -
    flips settlement.
    """

    def __init__(self, *snapshots: TransferStats) -> None:
        self._snapshots = list(snapshots)
        self._next_index = 0

    @property
    def done(self) -> bool:
        return self._next_index >= len(self._snapshots) - 1

    def stats(self) -> TransferStats:
        index = min(self._next_index, len(self._snapshots) - 1)
        snapshot = self._snapshots[index]
        if self._next_index < len(self._snapshots) - 1:
            self._next_index += 1
        return snapshot


class NeverDoneProgressSource:
    """A `ProgressSource` that never settles - `stats()` returns an
    ever-increasing snapshot forever."""

    def __init__(self) -> None:
        self._calls = 0

    @property
    def done(self) -> bool:
        return False

    def stats(self) -> TransferStats:
        self._calls += 1
        return _stats(self._calls)


class SlowStatsProgressSource:
    """A `ProgressSource` whose `stats()` blocks until `release()` is
    called - simulates a `stats()`/RC call that takes a while, to exercise
    `ProgressSubscription.stop()`'s `timeout` bound."""

    def __init__(self) -> None:
        self.entered_stats = threading.Event()
        self._release = threading.Event()

    @property
    def done(self) -> bool:
        return False

    def stats(self) -> TransferStats:
        self.entered_stats.set()
        self._release.wait(timeout=_WAIT_TIMEOUT)
        return _stats(1)

    def release(self) -> None:
        self._release.set()


class TestWatch:
    def test_watch_yields_every_snapshot_and_the_final_one_last(self) -> None:
        source = FakeProgressSource(_stats(10), _stats(50), _stats(100))

        results = list(watch(source, interval=0.0))

        assert [r.bytes for r in results] == [10, 50, 100]

    def test_watch_on_an_already_done_source_yields_once(self) -> None:
        source = FakeProgressSource(_stats(100))

        results = list(watch(source, interval=0.0))

        assert [r.bytes for r in results] == [100]

    def test_watch_sleeps_between_non_final_snapshots(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = FakeProgressSource(_stats(10), _stats(100))
        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", sleeps.append)

        list(watch(source, interval=0.25))

        assert sleeps == [0.25]


class TestOnProgress:
    def test_on_progress_calls_back_with_every_snapshot(self) -> None:
        source = FakeProgressSource(_stats(10), _stats(50), _stats(100))
        received: list[int] = []

        subscription = on_progress(source, lambda s: received.append(s.bytes), interval=0.0)
        _wait_until(lambda: len(received) == 3)
        subscription.stop()

        assert received == [10, 50, 100]

    def test_context_manager_exit_stops_the_subscription(self) -> None:
        source = FakeProgressSource(_stats(10), _stats(100))

        with on_progress(source, lambda _s: None, interval=0.01) as subscription:
            pass

        assert not subscription._thread.is_alive()

    def test_stop_is_idempotent(self) -> None:
        source = FakeProgressSource(_stats(100))
        subscription = on_progress(source, lambda _s: None, interval=0.01)

        subscription.stop()
        subscription.stop()  # must not raise or hang

    def test_a_callback_exception_is_swallowed_and_later_snapshots_still_arrive(self) -> None:
        source = FakeProgressSource(_stats(10), _stats(50), _stats(100))
        received: list[int] = []

        def _callback(snapshot: TransferStats) -> None:
            if snapshot.bytes == 50:
                raise RuntimeError("boom")
            received.append(snapshot.bytes)

        subscription = on_progress(source, _callback, interval=0.0)
        _wait_until(lambda: not subscription._thread.is_alive())
        subscription.stop()

        assert received == [10, 100]

    def test_stop_ends_a_never_settling_subscription_and_no_further_callbacks_follow(self) -> None:
        source = NeverDoneProgressSource()
        received: list[int] = []
        subscription = on_progress(source, lambda s: received.append(s.bytes), interval=0.01)

        _wait_until(lambda: len(received) >= 2)
        subscription.stop()

        assert not subscription._thread.is_alive()
        count_at_stop = len(received)
        time.sleep(0.05)
        assert len(received) == count_at_stop


class TestStopTimeoutAndReentrancy:
    def test_stop_with_a_generous_timeout_returns_true_once_the_thread_exits(self) -> None:
        source = FakeProgressSource(_stats(10), _stats(100))
        subscription = on_progress(source, lambda _s: None, interval=0.0)

        stopped = subscription.stop(timeout=_WAIT_TIMEOUT)

        assert stopped is True
        assert not subscription._thread.is_alive()

    def test_stop_with_a_short_timeout_returns_false_while_stats_is_still_blocking(self) -> None:
        source = SlowStatsProgressSource()
        subscription = on_progress(source, lambda _s: None, interval=0.0)
        assert source.entered_stats.wait(timeout=_WAIT_TIMEOUT)

        stopped = subscription.stop(timeout=0.05)

        assert stopped is False
        assert subscription._thread.is_alive()
        source.release()
        assert subscription.stop(timeout=_WAIT_TIMEOUT) is True  # clean shutdown afterward

    def test_stop_called_from_within_its_own_callback_does_not_raise_or_hang(self) -> None:
        # A thread can never join itself - stop() must detect this and skip
        # the join rather than let Thread.join() raise RuntimeError.
        source = FakeProgressSource(_stats(10), _stats(50), _stats(100))
        received: list[int] = []
        box: dict[str, ProgressSubscription] = {}

        def _callback(snapshot: TransferStats) -> None:
            received.append(snapshot.bytes)
            if snapshot.bytes == 50:
                assert box["subscription"].stop() is False  # can't confirm from inside itself

        subscription = on_progress(source, _callback, interval=0.05)
        box["subscription"] = subscription

        _wait_until(lambda: not subscription._thread.is_alive())

        assert received == [10, 50]  # stopped itself before the final snapshot arrived
