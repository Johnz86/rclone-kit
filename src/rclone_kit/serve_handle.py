"""`ServeHandle`: the execution-independent handle for one embedded
`serve/start` instance.

Deliberately minimal - `id`/`addr` plus disposal - since callers only ever
use it to keep the server alive and eventually shut it down, never for
protocol-specific operations. `HttpServer` wraps one of these;
`serve_webdav()` returns one directly.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Callable

    from rclone_kit.rc.serve import RcServeClient, ServeRef


class ServeHandle:
    """A handle to one running embedded `serve/start` instance.

    `dispose()` is idempotent and best-effort: a `serve/stop` failure
    (e.g. the server already stopped some other way) is swallowed rather
    than raised, matching `JobHandle.close()`'s own "shutdown is cleanup,
    not a place for a caller to catch a new error" philosophy. Idempotency
    also means `Rclone.close()` can unconditionally dispose every handle
    it tracked without needing to know whether a caller already disposed
    this one directly.

    `_on_dispose` (set by `Rclone._track_serve_handle`, never by a caller)
    removes this handle from the client's tracking set once disposed - the
    client only needs to track resources it hasn't yet disposed; without
    this, a client that starts and disposes many short-lived serve
    sessions over its lifetime would leak one entry per session forever.
    """

    def __init__(self, serve_client: RcServeClient, ref: ServeRef) -> None:
        self._serve_client = serve_client
        self._ref = ref
        self._closed = False
        self._on_dispose: Callable[[], None] | None = None

    @property
    def id(self) -> str:
        return self._ref.id

    @property
    def addr(self) -> str:
        return self._ref.addr

    @property
    def closed(self) -> bool:
        return self._closed

    def dispose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._serve_client.stop(self._ref.id)
        if self._on_dispose is not None:
            with contextlib.suppress(Exception):
                self._on_dispose()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.dispose()
