"""Consume a producer thread's output through a bounded queue.

A directory walk lists a remote far faster than a caller consumes the
result, and a whole tree does not fit in memory. Both `walk()` and
`scan_missing_folders()` therefore run their traversal on a daemon thread
that feeds a bounded `Queue`, and expose it as a generator that yields
whatever the thread has produced so far. This module holds the machinery
that lifecycle needs, so it is written and reasoned about once.
"""

import contextlib
import logging
from collections.abc import Callable, Generator
from queue import Queue
from threading import Thread

logger = logging.getLogger(__name__)

MAX_OUT_QUEUE_SIZE = 50
WORKER_JOIN_TIMEOUT_SECONDS = 30.0


def _drain_queue_until_sentinel[T](out_queue: Queue[T | None]) -> None:
    """Consume `out_queue` until the producer's sentinel `None` appears.

    Runs when a consumer stops iterating early (`break`, or garbage
    collection closing the generator) instead of letting the producer run
    to completion. Without this, the producer thread would block forever
    on `out_queue.put()` once nobody drains its bounded queue, leaking a
    permanently blocked thread.

    `KeyboardInterrupt` is suppressed *here only*, never in the consumer
    loop: this drain is the teardown itself, and abandoning it halfway
    recreates exactly the blocked thread it exists to prevent. An
    interrupt that started the teardown keeps propagating once this
    returns - only a further Ctrl+C landing inside the drain is dropped,
    and it costs at most the producer's remaining runtime, since the
    producer is already running and always ends by putting the sentinel.
    """
    with contextlib.suppress(KeyboardInterrupt):
        while out_queue.get() is not None:
            pass


def iter_background_producer[T](
    produce: Callable[[Queue[T | None]], None], *, description: str
) -> Generator[T]:
    """Yield everything `produce` puts on a bounded queue from its own thread.

    `produce` receives that queue and must put a `None` sentinel before
    returning, on success and on failure alike - the consumer loop below
    blocks on `out_queue.get()` forever otherwise. `description` names the
    caller in the worker's thread name and in the teardown warning.

    A failure inside `produce` is captured and re-raised from this
    generator, rather than signaled with `_thread.interrupt_main()`: that
    call delivers an async `KeyboardInterrupt` at the main thread's next
    bytecode boundary, which can land after this generator already
    exhausted normally - surfacing as a misleading `KeyboardInterrupt` in
    unrelated later code instead of the real failure.

    A `KeyboardInterrupt` raised in the consumer loop propagates rather
    than being swallowed: ending the generator normally instead would hand
    the caller a *silently truncated* result indistinguishable from a
    complete one. Teardown still runs in the `finally` below, so the
    producer thread is not leaked either way.
    """
    out_queue: Queue[T | None] = Queue(maxsize=MAX_OUT_QUEUE_SIZE)
    errors: list[BaseException] = []

    def task() -> None:
        try:
            produce(out_queue)
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=task, name=f"rclone-kit-{description}", daemon=True)
    worker.start()

    sentinel_seen = False
    try:
        while True:
            item = out_queue.get()
            if item is None:
                sentinel_seen = True
                break
            yield item
    finally:
        if not sentinel_seen:
            _drain_queue_until_sentinel(out_queue)
        worker.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS)
        if worker.is_alive():
            logger.warning(
                "%s background thread did not finish within %ss of generator teardown",
                description,
                WORKER_JOIN_TIMEOUT_SECONDS,
            )
    if errors:
        raise errors[0]
