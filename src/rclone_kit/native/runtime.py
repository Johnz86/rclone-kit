"""`RcloneRuntime`: one initialized native runtime with one immutable config
path, for the lifetime of the object.

Depends only on the small `NativeBinding` protocol below, not on `ctypes`
directly, so its lifecycle/state-machine logic can be unit tested against a
fake binding; `rclone_kit.native.abi.RcloneKitBinding` is the only production
implementation.
"""

import json
import logging
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

logger = logging.getLogger(__name__)

_CLOSE_DRAIN_LOG_INTERVAL_SECONDS = 10.0
"""How long `close()` waits between reporting that in-flight calls are
still holding up finalization. Not a deadline - the wait never gives up."""

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

    `call()` does NOT serialize concurrent RPC dispatch against other
    `call()`s: the Go bridge (`librclone/rclonekit/bridge.RPC`) only holds
    its own mutex briefly to check `initialized` before delegating to
    upstream `librclone.RPC`, which is safe to call concurrently from
    multiple goroutines/OS threads - the same way rclone's own RC HTTP
    server serves concurrent requests. Serializing every call through one
    Python-side lock would otherwise let one slow call (e.g. a blocking
    list-stream pull awaiting new items) delay every unrelated call for its
    full duration. `_state_lock` instead guards only the lifecycle
    transitions (`initialize`/`close`) and the in-flight call count:
    `close()` waits for every in-flight `call()` to finish before invoking
    `finalize()`, and no new `call()` can start once `close()` has begun.
    """

    def __init__(self, binding: NativeBinding) -> None:
        self._binding = binding
        self._state_lock = threading.Lock()
        self._no_calls_in_flight = threading.Condition(self._state_lock)
        self._active_calls = 0
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
        """Query build info. Safe to call before `initialize()`.

        Tracked as an in-flight call the same way `call()` is (see that
        method's docstring): without this, `close()` could observe
        `_active_calls == 0` and invoke `finalize()` while this method's own
        `RcloneKitBuildInfo` call is still running on the Go side - unlike
        `rpc()`, `RcloneKitBuildInfo`/`RcloneKitFinalize` concurrency isn't
        covered by the bridge mutex argument in the class docstring.
        """
        with self._state_lock:
            self._require_open()
            self._active_calls += 1
        try:
            status, output = self._binding.build_info()
        finally:
            with self._no_calls_in_flight:
                self._active_calls -= 1
                if self._active_calls == 0:
                    self._no_calls_in_flight.notify_all()
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
        with self._state_lock:
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

        Does not hold `_state_lock` for the duration of the underlying RPC
        dispatch (see the class docstring) - only to register/deregister
        this call as in-flight, so `close()` can wait for it to finish.
        """
        with self._state_lock:
            self._require_open()
            if not self._initialized:
                raise NativeNotInitializedError("call() before initialize()")
            self._active_calls += 1
        try:
            body = json.dumps(params or {}).encode("utf-8")
            status, output = self._binding.rpc(method.encode("utf-8"), body)
        finally:
            with self._no_calls_in_flight:
                self._active_calls -= 1
                if self._active_calls == 0:
                    self._no_calls_in_flight.notify_all()
        _raise_for_lifecycle_status(status, output)
        decoded = json.loads(output.decode("utf-8")) if output else {}
        return status, decoded

    def close(self) -> None:
        """Best-effort finalize and mark this runtime unusable. Idempotent.

        Blocks new `call()`s from starting immediately, then waits for
        every already-in-flight `call()` to finish before invoking
        `finalize()` - never interrupts one, since the underlying RC
        dispatch has no cancellation mechanism to interrupt it with.

        That wait is deliberately unbounded: finalizing the native runtime
        while a call is still dispatched inside it would tear down state
        that call is actively using, which is a process-level crash rather
        than a recoverable error. So there is no timeout that could safely
        give up and finalize anyway. What the wait does instead is
        *report* itself - a long synchronous operation (`check()` over a
        large tree, say) otherwise makes `close()` look like an
        unexplained hang with nothing in the log to explain it.
        """
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            was_initialized = self._initialized
            self._wait_for_calls_to_drain()
        if was_initialized:
            status, output = self._binding.finalize()
            if status != STATUS_OK:
                logger.warning(
                    "RcloneKitFinalize returned non-OK status %s: %s",
                    status,
                    output.decode("utf-8", errors="replace"),
                )

    def _wait_for_calls_to_drain(self) -> None:
        """Wait out every in-flight `call()`, logging while it takes long.

        Caller must hold `_state_lock` (which is `_no_calls_in_flight`'s
        own lock). Waits in bounded slices purely so a slow drain leaves a
        trail naming how many calls are still outstanding; the loop itself
        never gives up, for the reason `close()` documents.
        """
        waited = 0.0
        while not self._no_calls_in_flight.wait_for(
            lambda: self._active_calls == 0, timeout=_CLOSE_DRAIN_LOG_INTERVAL_SECONDS
        ):
            waited += _CLOSE_DRAIN_LOG_INTERVAL_SECONDS
            logger.warning(
                "close() has waited %.0fs for %d in-flight rclone call(s) to finish; "
                "the native runtime cannot be finalized until they return",
                waited,
                self._active_calls,
            )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeClosedError()


class _SharedRuntimeHolder:
    """Mutable holder for `shared_runtime()`'s process-wide instance.

    A plain class attribute (mutated in place, never rebound via `global`)
    rather than a module-level variable, so the double-checked-locking
    below only ever needs a normal attribute assignment.
    """

    lock: threading.Lock = threading.Lock()
    instance: "RcloneRuntime | None" = None


def shared_runtime(
    library_path: Path | None = None, config_path: Path | None = None
) -> RcloneRuntime:
    """Return the process-wide shared `RcloneRuntime`, creating and
    initializing it on the first call.

    `RcloneKitInitialize` is a once-per-*process* ABI operation (see the
    `RcloneRuntime` class docstring and `Rclone.__init__`'s own runtime-
    sharing note): loading the same shared library twice in one process
    reuses the same already-loaded module and its process-global Go
    runtime state, so a second, independently-initialized `RcloneRuntime`
    is never possible in-process, only ever a `NativeAlreadyInitializedError`.
    A production application that wants several `Rclone` clients (e.g. one
    per request or tenant) must therefore share one runtime, constructed
    exactly once, rather than let each client build its own - this
    function is that one construction point, made thread-safe so it can be
    called lazily from anywhere (module import time, a request handler,
    a worker thread) without the caller having to coordinate who goes
    first.

    Every call after the first returns that same already-initialized
    instance, regardless of the `library_path`/`config_path` passed -
    only the first caller's arguments take effect, matching `initialize()`'s
    own once-only semantics. Construct `Rclone(per_client_conf, runtime=
    shared_runtime())` for each client; each keeps its own job/serve/mount
    tracking and can be closed independently (per `Rclone.close()`'s
    injected-runtime handling) without affecting the others or the shared
    runtime itself. Close the shared runtime itself only at process
    shutdown, since doing so is irreversible for the rest of the process's
    life (see `RcloneRuntime.close()`).

    True isolation between clients (a hard security/tenancy boundary, not
    just independent config) is not possible in-process at all - every
    client sharing this runtime also shares its one immutable config path
    - and requires separate OS processes instead, each with its own single
    call to this function.
    """
    if _SharedRuntimeHolder.instance is not None:
        return _SharedRuntimeHolder.instance
    with _SharedRuntimeHolder.lock:
        if _SharedRuntimeHolder.instance is None:
            from rclone_kit.native.library import resolve_library_path

            runtime = RcloneRuntime.from_library_path(resolve_library_path(library_path))
            runtime.initialize(config_path=config_path)
            _SharedRuntimeHolder.instance = runtime
        return _SharedRuntimeHolder.instance
