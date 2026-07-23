"""RC list-stream boundary: typed `open`/`next`/`close` adapters over one
`RcCallable`, translating `rclonekit/liststream/*`'s wire JSON into plain
Python values (CLI-to-C-ABI migration Wave F design, section 4, decision
F1).

Wire shapes below were verified empirically against the pinned native
build (`native/rclone` at `6c929caad`), not merely assumed from the Go
source:

- `rclonekit/liststream/open` returns exactly `{"streamId": <int>}`;
- `rclonekit/liststream/next` returns `{"items": [...], "done": <bool>,
  "error": <str>}` - `items` entries are the same `Path`/`Name`/`Size`/
  `MimeType`/`ModTime`/`IsDir`/... shape `operations/list`'s own `list`
  array already uses (same underlying Go type), so no new item parser is
  needed - callers reuse `rclone_kit.file.FileItem.from_json`; and
- `rclonekit/liststream/close` returns `{}`, and is idempotent: closing an
  already-closed or unknown `streamId` is not an error.

This module is a leaf, matching `rc/jobs.py`'s own convention: it imports
no client/runtime modules, only `rc.errors`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rclone_kit.rc.client import RcCallable


@dataclass(frozen=True)
class ListStreamBatch:
    """One batch pulled from a list stream.

    `items` are raw wire dicts (not yet a domain type) - deliberately, so
    this module stays independent of `rclone_kit.file.FileItem`'s own
    `remote`/prefix conventions; the caller (which already knows the
    original `src` string) does that conversion.
    """

    items: tuple[Mapping[str, object], ...]
    done: bool
    error: str | None


class RcListStreamClient(Protocol):
    """Narrow list-stream interface the embedded stream wrapper depends
    on, so its tests can supply a fake without a real `RcClient`."""

    def open(
        self, fs: str, remote: str, opt: Mapping[str, object], config: Mapping[str, object]
    ) -> int: ...
    def next(self, stream_id: int, max_items: int, timeout_ms: int) -> ListStreamBatch: ...
    def close(self, stream_id: int) -> None: ...


def _require_int(payload: Mapping[str, object], field: str) -> int:
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an int, got {value!r}")
    return value


def _require_bool(payload: Mapping[str, object], field: str) -> bool:
    value = payload[field]
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a bool, got {value!r}")
    return value


def _require_str(payload: Mapping[str, object], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {value!r}")
    return value


def _parse_batch(payload: Mapping[str, object]) -> ListStreamBatch:
    items = payload["items"]
    if not isinstance(items, list):
        raise ValueError(f"items must be a list, got {items!r}")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"item entry must be an object, got {item!r}")
    error = _require_str(payload, "error") or None
    return ListStreamBatch(items=tuple(items), done=_require_bool(payload, "done"), error=error)


class RcloneRcListStreamClient:
    """The real `RcListStreamClient`, backed by one `RcCallable`."""

    def __init__(self, rc_client: RcCallable) -> None:
        self._rc_client = rc_client

    def open(
        self, fs: str, remote: str, opt: Mapping[str, object], config: Mapping[str, object]
    ) -> int:
        params: dict[str, object] = {"fs": fs, "remote": remote, "opt": dict(opt)}
        if config:
            params["_config"] = dict(config)
        response = self._rc_client.call("rclonekit/liststream/open", **params)
        return _require_int(response, "streamId")

    def next(self, stream_id: int, max_items: int, timeout_ms: int) -> ListStreamBatch:
        response = self._rc_client.call(
            "rclonekit/liststream/next",
            streamId=stream_id,
            maxItems=max_items,
            timeoutMs=timeout_ms,
        )
        return _parse_batch(response)

    def close(self, stream_id: int) -> None:
        self._rc_client.call("rclonekit/liststream/close", streamId=stream_id)
