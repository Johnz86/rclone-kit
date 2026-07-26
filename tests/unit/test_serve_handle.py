"""Unit tests for `ServeHandle`, driven by a fake `RcServeClient`."""

from rclone_kit.rc.serve import ServeRef
from rclone_kit.serve_handle import ServeHandle


class FakeServeClient:
    def __init__(self) -> None:
        self.stop_calls: list[str] = []
        self.raise_on_stop = False

    def start(self, serve_type: str, fs: str, addr: str, params) -> ServeRef:  # noqa: ARG002
        raise AssertionError("start() should not be called by ServeHandle itself")

    def stop(self, ref_id: str) -> None:
        self.stop_calls.append(ref_id)
        if self.raise_on_stop:
            raise RuntimeError("boom")

    def list(self) -> tuple[ServeRef, ...]:
        raise AssertionError("list() should not be called by ServeHandle itself")


def test_id_and_addr_expose_the_ref() -> None:
    client = FakeServeClient()
    handle = ServeHandle(client, ServeRef(id="http-1", addr="127.0.0.1:8080"))

    assert handle.id == "http-1"
    assert handle.addr == "127.0.0.1:8080"


def test_dispose_calls_stop_once() -> None:
    client = FakeServeClient()
    handle = ServeHandle(client, ServeRef(id="http-1", addr="127.0.0.1:8080"))

    handle.dispose()

    assert client.stop_calls == ["http-1"]
    assert handle.closed is True


def test_dispose_is_idempotent() -> None:
    client = FakeServeClient()
    handle = ServeHandle(client, ServeRef(id="http-1", addr="127.0.0.1:8080"))

    handle.dispose()
    handle.dispose()

    assert client.stop_calls == ["http-1"]


def test_dispose_swallows_stop_failures() -> None:
    client = FakeServeClient()
    client.raise_on_stop = True
    handle = ServeHandle(client, ServeRef(id="http-1", addr="127.0.0.1:8080"))

    handle.dispose()  # must not raise

    assert handle.closed is True


def test_context_manager_disposes_on_exit() -> None:
    client = FakeServeClient()

    with ServeHandle(client, ServeRef(id="http-1", addr="127.0.0.1:8080")) as handle:
        assert handle.closed is False

    assert handle.closed is True
    assert client.stop_calls == ["http-1"]
