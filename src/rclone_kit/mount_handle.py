"""`MountHandle`: the execution-independent handle for one embedded
`mount/mount` instance.

The mount lives inside this process's own WinFsp/FUSE (cgofuse) goroutine,
not a child process. `mount/mount`/`mount/unmount` start and tear down
that goroutine cleanly, so this handle only needs to remember which mount
point to pass back to `mount/unmount`. Mirrors `serve_handle.py`'s
`ServeHandle` shape.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Callable

    from rclone_kit.rc.mount import RcMountClient


class MountHandle:
    """A handle to one running embedded `mount/mount` instance.

    `dispose()` is idempotent and best-effort: a `mount/unmount` failure
    (e.g. the mount already went away some other way) is swallowed rather
    than raised, matching `ServeHandle.dispose()`'s own "shutdown is
    cleanup, not a place for a caller to catch a new error" philosophy.

    `_on_dispose` (set by `Rclone._track_mount_handle`, never by a caller)
    removes this handle from the client's tracking set once disposed - see
    `ServeHandle._on_dispose`'s docstring for why that matters.
    """

    def __init__(self, mount_client: RcMountClient, mount_point: str) -> None:
        self._mount_client = mount_client
        self._mount_point = mount_point
        self._closed = False
        self._on_dispose: Callable[[], None] | None = None

    @property
    def mount_path(self) -> Path:
        return Path(self._mount_point)

    @property
    def closed(self) -> bool:
        return self._closed

    def dispose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._mount_client.unmount(self._mount_point)
        if self._on_dispose is not None:
            with contextlib.suppress(Exception):
                self._on_dispose()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.dispose()

    def __hash__(self) -> int:
        return hash(self._mount_point)
