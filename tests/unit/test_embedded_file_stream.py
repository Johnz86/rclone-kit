"""Unit tests for `EmbeddedFilesStream`, driven by a fake
`RcListStreamClient` so these tests exercise batching/pagination/error
semantics without a built native library. Native-DLL parity is covered by
`tests/native/test_ls_stream_embedded_integration.py`.
"""

import pytest

from rclone_kit.embedded_file_stream import EmbeddedFilesStream
from rclone_kit.exceptions import RcloneCommandError
from rclone_kit.rc.list_stream import ListStreamBatch

_ITEM_A = {
    "Path": "a.txt",
    "Name": "a.txt",
    "Size": 1,
    "MimeType": "text/plain",
    "ModTime": "2024-01-01T00:00:00Z",
    "IsDir": False,
}
_ITEM_B = {
    "Path": "b.txt",
    "Name": "b.txt",
    "Size": 2,
    "MimeType": "text/plain",
    "ModTime": "2024-01-01T00:00:00Z",
    "IsDir": False,
}


class FakeListStreamClient:
    def __init__(self, batches: list[ListStreamBatch]) -> None:
        self._batches = list(batches)
        self.next_calls = 0
        self.closed_stream_ids: list[int] = []

    def open(self, fs: str, remote: str, opt, config) -> int:  # noqa: ARG002
        return 1

    def next(self, stream_id: int, max_items: int, timeout_ms: int) -> ListStreamBatch:  # noqa: ARG002
        self.next_calls += 1
        if not self._batches:
            return ListStreamBatch(items=(), done=True, error=None)
        return self._batches.pop(0)

    def close(self, stream_id: int) -> None:
        self.closed_stream_ids.append(stream_id)


def test_files_yields_items_across_multiple_batches() -> None:
    client = FakeListStreamClient(
        [
            ListStreamBatch(items=(_ITEM_A,), done=False, error=None),
            ListStreamBatch(items=(_ITEM_B,), done=True, error=None),
        ]
    )
    stream = EmbeddedFilesStream(client, "remote:base", stream_id=1)

    items = list(stream.files())

    assert [item.name for item in items] == ["a.txt", "b.txt"]


def test_files_stops_once_done_with_no_error() -> None:
    client = FakeListStreamClient([ListStreamBatch(items=(_ITEM_A,), done=True, error=None)])
    stream = EmbeddedFilesStream(client, "remote:base", stream_id=1)

    items = list(stream.files())

    assert len(items) == 1


def test_files_raises_rclone_command_error_when_stream_reports_an_error() -> None:
    client = FakeListStreamClient([ListStreamBatch(items=(), done=True, error="boom")])
    stream = EmbeddedFilesStream(client, "remote:base", stream_id=1)

    with pytest.raises(RcloneCommandError):
        list(stream.files())


def test_files_paged_batches_by_page_size() -> None:
    client = FakeListStreamClient(
        [
            ListStreamBatch(items=(_ITEM_A, _ITEM_B), done=False, error=None),
            ListStreamBatch(items=(_ITEM_A,), done=True, error=None),
        ]
    )
    stream = EmbeddedFilesStream(client, "remote:base", stream_id=1)

    pages = list(stream.files_paged(page_size=2))

    assert [len(page) for page in pages] == [2, 1]


def test_context_manager_closes_the_stream() -> None:
    client = FakeListStreamClient([ListStreamBatch(items=(), done=True, error=None)])

    with EmbeddedFilesStream(client, "remote:base", stream_id=1):
        pass

    assert client.closed_stream_ids == [1]


def test_close_is_idempotent() -> None:
    client = FakeListStreamClient([])
    stream = EmbeddedFilesStream(client, "remote:base", stream_id=1)

    stream.close()
    stream.close()

    assert client.closed_stream_ids == [1]


def test_iter_delegates_to_files() -> None:
    client = FakeListStreamClient([ListStreamBatch(items=(_ITEM_A,), done=True, error=None)])
    stream = EmbeddedFilesStream(client, "remote:base", stream_id=1)

    items = list(stream)

    assert [item.name for item in items] == ["a.txt"]
