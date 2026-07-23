"""`RcloneRuntime`: one initialized native runtime with one immutable config
path, for the lifetime of the object.

Depends only on the small `NativeBinding` protocol below, not on `ctypes`
directly, so its lifecycle/state-machine logic can be unit tested against a
fake binding; `rclone_kit.native.abi.RcloneKitBinding` is the only production
implementation.
"""

import json
import threading
from pathlib import Path
from typing import Protocol, Self

from rclone_kit.native.build_info import NativeBuildInfo, parse_build_info
from rclone_kit.native.errors import (
    AbiVersionMismatchError,
    NativeAlreadyInitializedError,
    NativeError,
    NativeInvalidInputError,
    NativeNotInitializedError,
    NativePanicError,
    RuntimeClosedError,
)

EXPECTED_ABI_VERSION = 1

STATUS_OK = 0
STATUS_INVALID_INPUT = -1
STATUS_NOT_INITIALIZED = -2
STATUS_ALREADY_INITIALIZED = -3
STATUS_PANIC = -4

_LIFECYCLE_ERRORS_BY_STATUS: dict[int, type[NativeError]] = {
    STATUS_INVALID_INPUT: NativeInvalidInputError,
    STATUS_NOT_INITIALIZED: NativeNotInitializedError,
    STATUS_ALREADY_INITIALIZED: NativeAlreadyInitializedError,
    STATUS_PANIC: NativePanicError,
}


class NativeBinding(Protocol):
    """The subset of `RcloneKitBinding`'s interface `RcloneRuntime` depends
    on. Every method takes/returns plain `bytes`; no pointers cross this
    boundary.
    """

    def abi_version(self) -> int: ...
    def build_info(self) -> tuple[int, bytes]: ...
    def initialize(self, payload: bytes) -> tuple[int, bytes]: ...
    def rpc(self, method: bytes, payload: bytes) -> tuple[int, bytes]: ...
    def finalize(self) -> tuple[int, bytes]: ...


def _raise_for_lifecycle_status(status: int, output: bytes) -> None:
    """Raise the typed error matching one of the ABI's reserved negative
    lifecycle status codes. A no-op for `STATUS_OK` and for any positive RC
    HTTP-like status, which `RcloneRuntime.call` decodes and returns as-is.
    """
    error_type = _LIFECYCLE_ERRORS_BY_STATUS.get(status)
    if error_type is None:
        return
    detail = output.decode("utf-8", errors="replace")
    raise error_type(detail)


class RcloneRuntime:
    """One loaded native library, initialized at most once, with one
    immutable config path for its whole lifetime.

    Not thread-safe across `initialize`/`close`; `call` serializes RPC
    dispatch with an internal lock so callers do not need their own.
    """

    def __init__(self, binding: NativeBinding) -> None:
        self._binding = binding
        self._lock = threading.Lock()
        self._initialized = False
        self._closed = False

    @classmethod
    def from_library_path(cls, library_path: Path) -> "RcloneRuntime":
        from rclone_kit.native.abi import RcloneKitBinding

        return cls(RcloneKitBinding(library_path))

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def closed(self) -> bool:
        return self._closed

    def build_info(self) -> NativeBuildInfo:
        """Query build info. Safe to call before `initialize()`."""
        self._require_open()
        status, output = self._binding.build_info()
        _raise_for_lifecycle_status(status, output)
        return parse_build_info(output)

    def initialize(self, config_path: Path | None = None) -> NativeBuildInfo:
        """Initialize the underlying library exactly once for this runtime.

        `config_path=None` uses rclone's default config discovery, matching
        `abi.h`'s documented `configPath` semantics. Raises
        `AbiVersionMismatchError` before attempting initialization if the
        loaded library reports a different ABI version than this binding was
        written against.
        """
        self._require_open()
        with self._lock:
            if self._initialized:
                raise NativeAlreadyInitializedError(
                    "initialize() already called on this RcloneRuntime"
                )
            abi_version = self._binding.abi_version()
            if abi_version != EXPECTED_ABI_VERSION:
                raise AbiVersionMismatchError(expected=EXPECTED_ABI_VERSION, actual=abi_version)
            payload = json.dumps(
                {"configPath": str(config_path) if config_path is not None else None}
            ).encode("utf-8")
            status, output = self._binding.initialize(payload)
            _raise_for_lifecycle_status(status, output)
            self._initialized = True
            return parse_build_info(output)

    def call(self, method: str, params: dict | None = None) -> tuple[int, dict]:
        """Execute one RC method. Returns `(status, decoded_json_body)` for
        every call that actually dispatched, including RC-level failures
        (rclone-kit's positive-status contract); only the ABI's own reserved
        negative lifecycle codes raise here.
        """
        self._require_open()
        if not self._initialized:
            raise NativeNotInitializedError("call() before initialize()")
        body = json.dumps(params or {}).encode("utf-8")
        with self._lock:
            status, output = self._binding.rpc(method.encode("utf-8"), body)
        _raise_for_lifecycle_status(status, output)
        decoded = json.loads(output.decode("utf-8")) if output else {}
        return status, decoded

    def close(self) -> None:
        """Best-effort finalize and mark this runtime unusable. Idempotent."""
        if self._closed:
            return
        with self._lock:
            if self._initialized:
                self._binding.finalize()
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeClosedError()
