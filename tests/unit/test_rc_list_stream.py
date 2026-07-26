"""Unit tests for `rclone_kit.rc.list_stream`'s RC list-stream boundary
(`RcloneRcListStreamClient`): request mapping and strict response parsing.

Uses a fake `RcCallable` driven by canned per-method responses, so these
tests exercise request/response mapping without a built native library.
Native-DLL shape verification is a separate concern; the shapes asserted
here were captured from ad hoc probes against the real built library
during development (see the module docstring in `rc/list_stream.py`).
"""

import pytest

from rclone_kit.rc.list_stream import ListStreamBatch, RcloneRcListStreamClient


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


class TestOpen:
    def test_open_sends_fs_remote_and_opt(self) -> None:
        client = FakeRcClient()
        client.queue("rclonekit/liststream/open", {"streamId": 7})
        stream_client = RcloneRcListStreamClient(client)

        stream_id = stream_client.open("remote:", "path", opt={"filesOnly": True}, config={})

        assert stream_id == 7
        assert client.calls == [
            (
                "rclonekit/liststream/open",
                {"fs": "remote:", "remote": "path", "opt": {"filesOnly": True}},
            )
        ]

    def test_open_includes_config_when_nonempty(self) -> None:
        client = FakeRcClient()
        client.queue("rclonekit/liststream/open", {"streamId": 1})
        stream_client = RcloneRcListStreamClient(client)

        stream_client.open("remote:", "", opt={}, config={"MaxDepth": 3})

        _method, params = client.calls[0]
        assert params["_config"] == {"MaxDepth": 3}

    def test_open_omits_config_when_empty(self) -> None:
        client = FakeRcClient()
        client.queue("rclonekit/liststream/open", {"streamId": 1})
        stream_client = RcloneRcListStreamClient(client)

        stream_client.open("remote:", "", opt={}, config={})

        _method, params = client.calls[0]
        assert "_config" not in params

    def test_open_boolean_stream_id_is_rejected(self) -> None:
        client = FakeRcClient()
        client.queue("rclonekit/liststream/open", {"streamId": True})
        stream_client = RcloneRcListStreamClient(client)

        with pytest.raises(ValueError, match="streamId"):
            stream_client.open("remote:", "", opt={}, config={})


class TestNext:
    def test_next_sends_stream_id_max_items_and_timeout(self) -> None:
        client = FakeRcClient()
        client.queue("rclonekit/liststream/next", {"items": [], "done": False, "error": ""})
        stream_client = RcloneRcListStreamClient(client)

        stream_client.next(7, 100, 500)

        assert client.calls == [
            ("rclonekit/liststream/next", {"streamId": 7, "maxItems": 100, "timeoutMs": 500})
        ]

    def test_next_parses_items_into_batch(self) -> None:
        client = FakeRcClient()
        item = {
            "Path": "a.txt",
            "Name": "a.txt",
            "Size": 5,
            "MimeType": "text/plain",
            "ModTime": "2024-01-01T00:00:00Z",
            "IsDir": False,
        }
        client.queue("rclonekit/liststream/next", {"items": [item], "done": False, "error": ""})
        stream_client = RcloneRcListStreamClient(client)

        batch = stream_client.next(7, 100, 500)

        assert batch == ListStreamBatch(items=(item,), done=False, error=None)

    def test_next_empty_error_string_becomes_none(self) -> None:
        client = FakeRcClient()
        client.queue("rclonekit/liststream/next", {"items": [], "done": True, "error": ""})
        stream_client = RcloneRcListStreamClient(client)

        batch = stream_client.next(7, 100, 500)

        assert batch.error is None

    def test_next_carries_a_real_error(self) -> None:
        client = FakeRcClient()
        client.queue("rclonekit/liststream/next", {"items": [], "done": True, "error": "boom"})
        stream_client = RcloneRcListStreamClient(client)

        batch = stream_client.next(7, 100, 500)

        assert batch.done is True
        assert batch.error == "boom"

    def test_next_rejects_non_list_items(self) -> None:
        client = FakeRcClient()
        client.queue(
            "rclonekit/liststream/next", {"items": "not a list", "done": False, "error": ""}
        )
        stream_client = RcloneRcListStreamClient(client)

        with pytest.raises(ValueError, match="items"):
            stream_client.next(7, 100, 500)

    def test_next_rejects_non_object_item_entries(self) -> None:
        client = FakeRcClient()
        client.queue(
            "rclonekit/liststream/next", {"items": ["not an object"], "done": False, "error": ""}
        )
        stream_client = RcloneRcListStreamClient(client)

        with pytest.raises(ValueError, match="item entry"):
            stream_client.next(7, 100, 500)


class TestClose:
    def test_close_sends_stream_id(self) -> None:
        client = FakeRcClient()
        client.queue("rclonekit/liststream/close", {})
        stream_client = RcloneRcListStreamClient(client)

        stream_client.close(7)

        assert client.calls == [("rclonekit/liststream/close", {"streamId": 7})]
