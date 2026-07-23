"""Unit tests for `rclone_kit.native.runtime.RcloneRuntime`.

Uses a fake `NativeBinding` implementation instead of a real `ctypes`-loaded
shared library, so these tests exercise the lifecycle state machine (ABI
version check, initialize-once, use-after-close, lifecycle-status-to-
exception mapping) without needing a built native library on disk. The real
`ctypes` plumbing is proven separately by
`tests/native/test_native_runtime_integration.py` against an actual built
`librclone_kit` library.
"""

import json
from pathlib import Path

import pytest

from rclone_kit.native.build_info import NativeBuildInfo
from rclone_kit.native.errors import (
    AbiVersionMismatchError,
    NativeAlreadyInitializedError,
    NativeInvalidInputError,
    NativeNotInitializedError,
    NativePanicError,
    RuntimeClosedError,
)
from rclone_kit.native.runtime import RcloneRuntime

_SAMPLE_BUILD_INFO_JSON = json.dumps(
    {
        "abiVersion": 1,
        "rcloneVersion": "v1.75.0-DEV",
        "rcloneCommit": "abc123",
        "goVersion": "go1.26.5",
        "buildTags": [],
        "target": "windows/amd64",
    }
).encode("utf-8")


class FakeBinding:
    """A fake `NativeBinding` driven entirely by canned responses, so tests
    can force every ABI status path without a real shared library.
    """

    def __init__(self, *, abi_version: int = 1) -> None:
        self._abi_version = abi_version
        self.finalize_calls = 0
        self.rpc_calls: list[tuple[bytes, bytes]] = []
        self.next_rpc_response: tuple[int, bytes] = (200, b"{}")
        self.build_info_response: tuple[int, bytes] = (0, _SAMPLE_BUILD_INFO_JSON)
        self.initialize_response: tuple[int, bytes] = (0, _SAMPLE_BUILD_INFO_JSON)

    def abi_version(self) -> int:
        return self._abi_version

    def build_info(self) -> tuple[int, bytes]:
        return self.build_info_response

    def initialize(self, payload: bytes) -> tuple[int, bytes]:
        self.last_initialize_payload = payload
        return self.initialize_response

    def rpc(self, method: bytes, payload: bytes) -> tuple[int, bytes]:
        self.rpc_calls.append((method, payload))
        return self.next_rpc_response

    def finalize(self) -> tuple[int, bytes]:
        self.finalize_calls += 1
        return 0, b"{}"


def test_build_info_works_before_initialize() -> None:
    runtime = RcloneRuntime(FakeBinding())
    info = runtime.build_info()
    assert info == NativeBuildInfo(
        abi_version=1,
        rclone_version="v1.75.0-DEV",
        rclone_commit="abc123",
        go_version="go1.26.5",
        build_tags=(),
        target="windows/amd64",
    )


def test_initialize_rejects_abi_version_mismatch() -> None:
    runtime = RcloneRuntime(FakeBinding(abi_version=2))
    with pytest.raises(AbiVersionMismatchError) as excinfo:
        runtime.initialize()
    assert excinfo.value.expected == 1
    assert excinfo.value.actual == 2
    assert not runtime.initialized


def test_initialize_succeeds_and_sets_initialized_flag() -> None:
    runtime = RcloneRuntime(FakeBinding())
    info = runtime.initialize()
    assert runtime.initialized
    assert info.rclone_version == "v1.75.0-DEV"


def test_initialize_twice_raises_already_initialized() -> None:
    runtime = RcloneRuntime(FakeBinding())
    runtime.initialize()
    with pytest.raises(NativeAlreadyInitializedError):
        runtime.initialize()


def test_initialize_encodes_missing_config_path_as_null() -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)
    runtime.initialize(config_path=None)
    assert json.loads(binding.last_initialize_payload) == {"configPath": None}


def test_initialize_encodes_explicit_config_path_as_string(tmp_path: Path) -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)
    config_path = tmp_path / "rclone.conf"
    runtime.initialize(config_path=config_path)
    assert json.loads(binding.last_initialize_payload) == {"configPath": str(config_path)}


def test_call_before_initialize_raises_not_initialized() -> None:
    runtime = RcloneRuntime(FakeBinding())
    with pytest.raises(NativeNotInitializedError):
        runtime.call("core/version")


def test_call_after_initialize_returns_status_and_decoded_json() -> None:
    binding = FakeBinding()
    binding.next_rpc_response = (200, json.dumps({"version": "v1.75.0"}).encode("utf-8"))
    runtime = RcloneRuntime(binding)
    runtime.initialize()

    status, payload = runtime.call("core/version", {"foo": "bar"})

    assert status == 200
    assert payload == {"version": "v1.75.0"}
    assert binding.rpc_calls == [(b"core/version", json.dumps({"foo": "bar"}).encode("utf-8"))]


def test_call_passes_through_non_success_rc_status() -> None:
    binding = FakeBinding()
    binding.next_rpc_response = (500, json.dumps({"error": "boom"}).encode("utf-8"))
    runtime = RcloneRuntime(binding)
    runtime.initialize()

    status, payload = runtime.call("operations/list")

    assert status == 500
    assert payload == {"error": "boom"}


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (-1, NativeInvalidInputError),
        (-2, NativeNotInitializedError),
        (-3, NativeAlreadyInitializedError),
        (-4, NativePanicError),
    ],
)
def test_call_raises_typed_error_for_reserved_lifecycle_status(
    status: int, error_type: type[Exception]
) -> None:
    binding = FakeBinding()
    binding.next_rpc_response = (status, b'{"error": "detail"}')
    runtime = RcloneRuntime(binding)
    runtime.initialize()

    with pytest.raises(error_type):
        runtime.call("core/version")


def test_close_is_idempotent_and_calls_finalize_once() -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)
    runtime.initialize()

    runtime.close()
    runtime.close()

    assert binding.finalize_calls == 1
    assert runtime.closed


def test_close_without_initialize_does_not_call_finalize() -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)
    runtime.close()
    assert binding.finalize_calls == 0


def test_use_after_close_raises_runtime_closed_error() -> None:
    runtime = RcloneRuntime(FakeBinding())
    runtime.initialize()
    runtime.close()

    with pytest.raises(RuntimeClosedError):
        runtime.call("core/version")
    with pytest.raises(RuntimeClosedError):
        runtime.build_info()
    with pytest.raises(RuntimeClosedError):
        runtime.initialize()


def test_context_manager_closes_on_exit() -> None:
    binding = FakeBinding()
    with RcloneRuntime(binding) as runtime:
        runtime.initialize()
    assert binding.finalize_calls == 1
    assert runtime.closed
