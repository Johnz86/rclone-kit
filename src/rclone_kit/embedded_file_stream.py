"""Embedded-execution counterpart to `file_stream.FilesStream` (CLI-to-C-
ABI migration Wave F design, section 4/5, decisions F1-F4/F7).

Pulls items from a `rclonekit/liststream/*` cursor (bounded, backpressured
- see `rc/list_stream.py`) instead of parsing a subprocess's stdout, but
exposes the exact same public surface (`files()`, `files_paged()`, context-
manager close) so `Rclone.save_to_db()` and any other `FilesStream`
consumer need no changes to work under `execution="embedded"`.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Self

from rclone_kit.exceptions import RcloneCommandError
from rclone_kit.file import FileItem

if TYPE_CHECKING:
    from rclone_kit.rc.list_stream import RcListStreamClient

_BATCH_SIZE = 1000
_POLL_TIMEOUT_MS = 500


class EmbeddedFilesStream:
    """A `rclonekit/liststream/*`-backed stream of `FileItem`s under `path`.

    Unlike `FilesStream` (which never surfaces a failed `lsjson`
    subprocess as an exception - a pre-existing CLI-backend gap), a
    listing failure here raises `RcloneCommandError` once the generator
    reaches the point where the stream reports it, since silently
    swallowing a real failure would be a worse contract to introduce anew.
    """

    def __init__(self, list_stream_client: RcListStreamClient, path: str, stream_id: int) -> None:
        self.path = path
        self._client = list_stream_client
        self._stream_id = stream_id
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Idempotent: closing an already-closed or naturally-exhausted
        stream is a no-op, matching `rclonekit/liststream/close`'s own
        idempotent contract."""
        if not self._closed:
            self._closed = True
            self._client.close(self._stream_id)

    def files(self) -> Generator[FileItem]:
        while True:
            batch = self._client.next(self._stream_id, _BATCH_SIZE, _POLL_TIMEOUT_MS)
            for item in batch.items:
                file_item = FileItem.from_json(self.path, dict(item))
                if file_item is not None:
                    yield file_item
            if batch.done:
                if batch.error:
                    raise RcloneCommandError("liststream", batch.error, RuntimeError(batch.error))
                return

    def files_paged(self, page_size: int = 1000) -> Generator[list[FileItem]]:
        page: list[FileItem] = []
        for fileitem in self.files():
            page.append(fileitem)
            if len(page) >= page_size:
                yield page
                page = []
        if len(page) > 0:
            yield page

    def __iter__(self) -> Generator[FileItem]:
        return self.files()
