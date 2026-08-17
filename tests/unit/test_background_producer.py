"""Unit tests for `rclone_kit.background_producer.iter_background_producer`,
the bounded-queue background-thread lifecycle that `walk()` and
`scan_missing_folders()` both run on.

The producer here deliberately puts more items than the queue holds, so
every test exercises a producer that is actually blocked on `put()` at the
moment the consumer stops reading.
"""

from collections.abc import Callable
from queue import Queue
from threading import Thread

import pytest

from rclone_kit import background_producer as background_producer_module
from rclone_kit.background_producer import MAX_OUT_QUEUE_SIZE, iter_background_producer

_OVERFLOWING_ITEM_COUNT = MAX_OUT_QUEUE_SIZE * 3
_PRODUCER_DESCRIPTION = "unit_test_producer"


class _TrackingThread(Thread):
    def __init__(self, *, target: Callable[[], object], name: str, daemon: bool) -> None:
        super().__init__(target=target, name=name, daemon=daemon)
        _CREATED_THREADS.append(self)


_CREATED_THREADS: list[Thread] = []


@pytest.fixture(autouse=True)
def _track_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    _CREATED_THREADS.clear()
    monkeypatch.setattr(background_producer_module, "Thread", _TrackingThread)


def _produce_more_than_the_queue_holds(out_queue: Queue[int | None]) -> None:
    try:
        for item in range(_OVERFLOWING_ITEM_COUNT):
            out_queue.put(item)
    finally:
        out_queue.put(None)


def _produce_then_fail(out_queue: Queue[int | None]) -> None:
    try:
        out_queue.put(0)
        raise RuntimeError("simulated producer failure")
    finally:
        out_queue.put(None)


def test_yields_everything_the_producer_puts_in_order() -> None:
    items = list(
        iter_background_producer(
            _produce_more_than_the_queue_holds, description=_PRODUCER_DESCRIPTION
        )
    )

    assert items == list(range(_OVERFLOWING_ITEM_COUNT))
    assert len(_CREATED_THREADS) == 1
    assert not _CREATED_THREADS[0].is_alive()


def test_early_close_drains_the_queue_instead_of_leaking_a_blocked_producer() -> None:
    generator = iter_background_producer(
        _produce_more_than_the_queue_holds, description=_PRODUCER_DESCRIPTION
    )
    assert next(generator) == 0

    generator.close()

    assert len(_CREATED_THREADS) == 1
    assert not _CREATED_THREADS[0].is_alive()


def test_producer_failure_is_reraised_to_the_consumers_caller() -> None:
    generator = iter_background_producer(_produce_then_fail, description=_PRODUCER_DESCRIPTION)

    with pytest.raises(RuntimeError, match="simulated producer failure"):
        list(generator)

    assert not _CREATED_THREADS[0].is_alive()


def test_consumer_keyboard_interrupt_propagates_and_still_joins_the_producer() -> None:
    generator = iter_background_producer(
        _produce_more_than_the_queue_holds, description=_PRODUCER_DESCRIPTION
    )
    assert next(generator) == 0

    with pytest.raises(KeyboardInterrupt):
        generator.throw(KeyboardInterrupt)

    assert len(_CREATED_THREADS) == 1
    assert not _CREATED_THREADS[0].is_alive()


def test_worker_thread_is_named_after_its_caller() -> None:
    list(
        iter_background_producer(
            _produce_more_than_the_queue_holds, description=_PRODUCER_DESCRIPTION
        )
    )

    assert _CREATED_THREADS[0].name == f"rclone-kit-{_PRODUCER_DESCRIPTION}"
