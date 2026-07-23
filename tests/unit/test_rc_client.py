"""Unit tests for `rclone_kit.rc.client.RcClient`."""

import pytest

from rclone_kit.rc.client import RcClient
from rclone_kit.rc.errors import RcCallError


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self.next_response: tuple[int, dict] = (200, {})

    def call(self, method: str, params: dict | None = None) -> tuple[int, dict]:
        self.calls.append((method, params))
        return self.next_response


def test_call_returns_payload_on_success() -> None:
    runtime = FakeRuntime()
    runtime.next_response = (200, {"version": "v1.75.0"})
    client = RcClient(runtime)

    result = client.call("core/version")

    assert result == {"version": "v1.75.0"}
    assert runtime.calls == [("core/version", {})]


def test_call_forwards_keyword_params() -> None:
    runtime = FakeRuntime()
    client = RcClient(runtime)

    client.call("operations/list", fs=":memory:", remote="bucket")

    assert runtime.calls == [("operations/list", {"fs": ":memory:", "remote": "bucket"})]


def test_call_raises_rc_call_error_on_non_success_status() -> None:
    runtime = FakeRuntime()
    runtime.next_response = (500, {"error": "boom"})
    client = RcClient(runtime)

    with pytest.raises(RcCallError) as excinfo:
        client.call("operations/list")

    assert excinfo.value.method == "operations/list"
    assert excinfo.value.status == 500
    assert excinfo.value.payload == {"error": "boom"}
