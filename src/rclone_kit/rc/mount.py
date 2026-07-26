"""RC mount boundary: typed `mount`/`unmount` adapters over one
`RcCallable`, translating `mount/mount`/`mount/unmount`'s wire JSON into
plain Python values.

Wire shapes verified empirically against the pinned native build (built
with `-tags cmount` and the installed WinFsp SDK's `CPATH`) and by reading
`native/rclone/cmd/mountlib/rc.go` directly:

- `mount/mount` takes `fs` (required), `mountPoint` (required), and
  optionally `mountOpt`/`vfsOpt` (each a JSON *object*, decoded with
  standard Go JSON field names like `"CacheMode"`/`"ReadOnly"` - NOT the
  underscored `config:` tags like `vfs_cache_mode` used everywhere else in
  this project's flat `_config`-style RC params) plus any flat `_config`
  option (e.g. `transfers`), and returns `{"mountPoint": <str>}` - the
  *actual* mount point used, which may differ from the input (e.g. `"*"`
  auto-assigning a drive letter on Windows); and
- `mount/unmount` takes `{"mountPoint": <str>}` and returns `{}`.

This module is a leaf, matching `rc/serve.py`/`rc/list_stream.py`'s own
convention: it imports no client/runtime modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rclone_kit.rc.client import RcCallable


@dataclass(frozen=True)
class MountRef:
    """Identifies one running `mount/mount` instance."""

    mount_point: str


class RcMountClient(Protocol):
    """Narrow mount-control interface `MountHandle` depends on, so its
    tests can supply a fake without a real `RcClient`."""

    def mount(
        self,
        fs: str,
        mount_point: str,
        *,
        vfs_opt: Mapping[str, object] | None = None,
        mount_opt: Mapping[str, object] | None = None,
        config: Mapping[str, object] | None = None,
    ) -> MountRef: ...
    def unmount(self, mount_point: str) -> None: ...


def _require_str(payload: Mapping[str, object], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {value!r}")
    return value


class RcloneRcMountClient:
    """The real `RcMountClient`, backed by one `RcCallable`."""

    def __init__(self, rc_client: RcCallable) -> None:
        self._rc_client = rc_client

    def mount(
        self,
        fs: str,
        mount_point: str,
        *,
        vfs_opt: Mapping[str, object] | None = None,
        mount_opt: Mapping[str, object] | None = None,
        config: Mapping[str, object] | None = None,
    ) -> MountRef:
        params: dict[str, object] = {"fs": fs, "mountPoint": mount_point}
        if vfs_opt:
            params["vfsOpt"] = dict(vfs_opt)
        if mount_opt:
            params["mountOpt"] = dict(mount_opt)
        if config:
            params.update(config)
        response = self._rc_client.call("mount/mount", **params)
        return MountRef(mount_point=_require_str(response, "mountPoint"))

    def unmount(self, mount_point: str) -> None:
        self._rc_client.call("mount/unmount", mountPoint=mount_point)
