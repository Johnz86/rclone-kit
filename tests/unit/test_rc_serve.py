"""Unit tests for `rclone_kit.rc.serve`'s RC serve boundary
(`RcloneRcServeClient`): request mapping and strict response parsing.

Uses a fake `RcCallable` driven by canned per-method responses, so these
tests exercise request/response mapping without a built native library.
The shapes asserted here were captured from ad hoc probes against the
real built library during development (see the module docstring in
`rc/serve.py`).
"""

import pytest

from rclone_kit.rc.serve import RcloneRcServeClient, ServeRef


class FakeRcClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._queues: dict[str, list[dict]] = {}

    def queue(self, method: str, *responses: dict) -> None:
        self._queues[method] = list(responses)

    def call(self, method: str, **params: object) -> dict:
        self.calls.append((method, params))
        queue = self._queues.get(method)
        if not queue:
            raise AssertionError(f"no queued response for {method!r}")
        return queue[0] if len(queue) == 1 else queue.pop(0)


class TestStart:
    def test_start_sends_type_fs_addr_and_extra_params(self) -> None:
        client = FakeRcClient()
        client.queue("serve/start", {"id": "http-1", "addr": "127.0.0.1:8080"})
        serve_client = RcloneRcServeClient(client)

        ref = serve_client.start("http", "remote:base", "127.0.0.1:0", {"vfs_cache_mode": "full"})

        assert ref == ServeRef(id="http-1", addr="127.0.0.1:8080")
        assert client.calls == [
            (
                "serve/start",
                {
                    "type": "http",
                    "fs": "remote:base",
                    "addr": "127.0.0.1:0",
                    "vfs_cache_mode": "full",
                },
            )
        ]

    def test_start_returned_addr_may_differ_from_requested(self) -> None:
        client = FakeRcClient()
        client.queue("serve/start", {"id": "http-1", "addr": "127.0.0.1:54321"})
        serve_client = RcloneRcServeClient(client)

        ref = serve_client.start("http", "remote:base", "127.0.0.1:0", {})

        assert ref.addr == "127.0.0.1:54321"

    def test_start_boolean_id_is_rejected(self) -> None:
        client = FakeRcClient()
        client.queue("serve/start", {"id": True, "addr": "127.0.0.1:8080"})
        serve_client = RcloneRcServeClient(client)

        with pytest.raises(ValueError, match="id"):
            serve_client.start("http", "remote:base", "127.0.0.1:0", {})


class TestStop:
    def test_stop_sends_id(self) -> None:
        client = FakeRcClient()
        client.queue("serve/stop", {})
        serve_client = RcloneRcServeClient(client)

        serve_client.stop("http-1")

        assert client.calls == [("serve/stop", {"id": "http-1"})]


class TestList:
    def test_list_parses_multiple_entries(self) -> None:
        client = FakeRcClient()
        client.queue(
            "serve/list",
            {
                "list": [
                    {"id": "http-1", "addr": "127.0.0.1:1", "params": {}},
                    {"id": "webdav-1", "addr": "127.0.0.1:2", "params": {}},
                ]
            },
        )
        serve_client = RcloneRcServeClient(client)

        refs = serve_client.list()

        assert refs == (
            ServeRef(id="http-1", addr="127.0.0.1:1"),
            ServeRef(id="webdav-1", addr="127.0.0.1:2"),
        )

    def test_list_empty(self) -> None:
        client = FakeRcClient()
        client.queue("serve/list", {"list": []})
        serve_client = RcloneRcServeClient(client)

        assert serve_client.list() == ()

    def test_list_rejects_non_list(self) -> None:
        client = FakeRcClient()
        client.queue("serve/list", {"list": "not a list"})
        serve_client = RcloneRcServeClient(client)

        with pytest.raises(ValueError, match="list"):
            serve_client.list()
