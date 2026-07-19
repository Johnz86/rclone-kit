"""Unit tests for `rclone_kit.fs.filesystem.RemoteFS`'s resource-ownership
lifecycle: context-manager support delegating to the existing idempotent
`dispose()`, matching the pattern already used by its sibling resource
owners `Mount` and `HttpServer`.

`RemoteFS.__init__` spawns a real HTTP server process, so these tests
build an instance via `object.__new__` and set only the attributes
`dispose()` reads, rather than constructing one for real.
"""

from typing import cast

from rclone_kit.fs.filesystem import RemoteFS
from rclone_kit.http_server import HttpServer


class _FakeServer:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def _stub_remote_fs() -> tuple[RemoteFS, _FakeServer]:
    fs = object.__new__(RemoteFS)
    fake_server = _FakeServer()
    fs.shutdown = False
    fs.server = cast(HttpServer, fake_server)
    return fs, fake_server


def test_context_manager_disposes_server_on_exit() -> None:
    fs, fake_server = _stub_remote_fs()

    with fs as entered:
        assert entered is fs

    assert fs.shutdown is True
    assert fake_server.shutdown_calls == 1


def test_context_manager_dispose_is_idempotent_with_explicit_dispose() -> None:
    fs, fake_server = _stub_remote_fs()

    with fs:
        pass
    fs.dispose()

    assert fake_server.shutdown_calls == 1
