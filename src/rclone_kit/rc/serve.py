"""RC serve boundary: typed `start`/`stop`/`list` adapters over one
`RcCallable`, translating `serve/start`/`serve/stop`/`serve/list`'s wire
JSON into plain Python values.

Wire shapes below were verified empirically against the pinned native
build (the pinned `native/rclone` submodule commit), not merely assumed from the RC
help text:

- `serve/start` (`type`, `fs`, `addr`, plus flag-derived parameters using
  the same `--foo-bar` -> `foo_bar` convention as any other RC call)
  returns exactly `{"addr": <str>, "id": <str>}` - `addr` is the actual
  bound address, which may differ from the requested one (e.g. a
  requested port `0` is resolved to the real ephemeral port actually
  bound);
- `serve/stop` takes `{"id": <str>}` and returns `{}`; and
- `serve/list` takes no parameters and returns `{"list": [{"id", "addr",
  "params"}, ...]}`.

This module is a leaf, matching `rc/jobs.py`/`rc/list_stream.py`'s own
convention: it imports no client/runtime modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rclone_kit.rc.client import RcCallable


@dataclass(frozen=True)
class ServeRef:
    """Identifies one running `serve/start` instance."""

    id: str
    addr: str


class RcServeClient(Protocol):
    """Narrow serve-control interface `ServeHandle` depends on, so its
    tests can supply a fake without a real `RcClient`."""

    def start(
        self, serve_type: str, fs: str, addr: str, params: Mapping[str, object]
    ) -> ServeRef: ...
    def stop(self, ref_id: str) -> None: ...
    def list(self) -> tuple[ServeRef, ...]: ...


def _require_str(payload: Mapping[str, object], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {value!r}")
    return value


class RcloneRcServeClient:
    """The real `RcServeClient`, backed by one `RcCallable`."""

    def __init__(self, rc_client: RcCallable) -> None:
        self._rc_client = rc_client

    def start(self, serve_type: str, fs: str, addr: str, params: Mapping[str, object]) -> ServeRef:
        response = self._rc_client.call(
            "serve/start", type=serve_type, fs=fs, addr=addr, **dict(params)
        )
        return ServeRef(id=_require_str(response, "id"), addr=_require_str(response, "addr"))

    def stop(self, ref_id: str) -> None:
        self._rc_client.call("serve/stop", id=ref_id)

    def list(self) -> tuple[ServeRef, ...]:
        response = self._rc_client.call("serve/list")
        items = response.get("list", [])
        if not isinstance(items, list):
            raise ValueError(f"list must be a list, got {items!r}")
        return tuple(
            ServeRef(id=_require_str(item, "id"), addr=_require_str(item, "addr")) for item in items
        )
