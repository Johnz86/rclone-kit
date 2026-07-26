"""Typed JSON RC call boundary over one `RcloneRuntime`.

Receives and returns plain Python `dict`/`str` values; never imports
`ctypes` or touches a raw pointer. Operation-level adapters depend on this,
not on `rclone_kit.native` directly.
"""

from typing import Protocol

from rclone_kit.rc.errors import RcCallError

_OK_STATUS = 200


class RcCapableRuntime(Protocol):
    """The subset of `RcloneRuntime`'s interface `RcClient` depends on, so
    tests can supply a fake without constructing a real native runtime.
    """

    def call(self, method: str, params: dict | None = None) -> tuple[int, dict]: ...


class RcCallable(Protocol):
    """The subset of `RcClient`'s own interface operation-level adapters
    (`operations/*_embedded.py`) depend on, so their tests can supply a fake
    without constructing a real `RcClient`/`RcloneRuntime` pair.
    """

    def call(self, method: str, **params: object) -> dict: ...


class RcClient:
    """Calls one RC method per invocation and raises `RcCallError` unless
    rclone reports success.
    """

    def __init__(self, runtime: RcCapableRuntime) -> None:
        self._runtime = runtime

    def call(self, method: str, **params: object) -> dict:
        """Execute `method` with `params` as the RC call's JSON body.

        Raises `RcCallError` when the call dispatched but did not return the
        expected success status; the ABI's own reserved negative lifecycle
        codes are raised directly by the underlying `RcloneRuntime.call`.
        """
        status, payload = self._runtime.call(method, params)
        if status != _OK_STATUS:
            raise RcCallError(method, status, payload)
        return payload
