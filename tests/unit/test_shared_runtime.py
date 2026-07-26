"""Unit tests for `rclone_kit.native.runtime.shared_runtime()` (finding
#2, "process-global runtime ownership"): a thread-safe, initialize-once
accessor for the one `RcloneRuntime` a multi-client production process can
ever have.
"""

import threading
from pathlib import Path

import pytest

from rclone_kit.native.runtime import RcloneRuntime, _SharedRuntimeHolder, shared_runtime


class FakeBinding:
    """A minimal fake `NativeBinding`, driven entirely by canned success
    responses - `test_native_runtime.py` covers every ABI status path in
    depth; this file only needs `shared_runtime()`'s own construction and
    singleton behavior."""

    def __init__(self) -> None:
        self.finalize_calls = 0

    def abi_version(self) -> int:
        return 1

    def build_info(self) -> tuple[int, bytes]:
        return (
            0,
            b'{"abiVersion":1,"rcloneVersion":"v1","rcloneCommit":"c","goVersion":"go1",'
            b'"buildTags":[],"target":"windows/amd64"}',
        )

    def initialize(self, payload: bytes) -> tuple[int, bytes]:
        del payload
        return self.build_info()

    def rpc(self, method: bytes, payload: bytes) -> tuple[int, bytes]:
        del method, payload
        return 200, b"{}"

    def finalize(self) -> tuple[int, bytes]:
        self.finalize_calls += 1
        return 0, b"{}"


@pytest.fixture(autouse=True)
def _reset_shared_runtime_holder():
    """Every test starts and ends with no process-wide instance set - this
    module-level singleton would otherwise leak across tests in the same
    pytest process."""
    _SharedRuntimeHolder.instance = None
    yield
    _SharedRuntimeHolder.instance = None


def _patch_binding(monkeypatch: pytest.MonkeyPatch) -> FakeBinding:
    binding = FakeBinding()
    monkeypatch.setattr(
        "rclone_kit.native.library.resolve_library_path", lambda *_a, **_kw: Path("fake-library")
    )
    monkeypatch.setattr(RcloneRuntime, "from_library_path", lambda _path: RcloneRuntime(binding))
    return binding


def test_first_call_creates_and_initializes_a_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_binding(monkeypatch)

    runtime = shared_runtime()

    assert runtime.initialized


def test_second_call_returns_the_same_instance_without_reinitializing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _patch_binding(monkeypatch)

    first = shared_runtime()
    second = shared_runtime()

    assert first is second
    assert binding.finalize_calls == 0
    # only the first call's construction actually dispatched an initialize
    assert first.initialized


def test_concurrent_first_calls_create_exactly_one_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_binding(monkeypatch)
    results: list[RcloneRuntime] = []
    barrier = threading.Barrier(8)

    def _call() -> None:
        barrier.wait(timeout=2.0)
        results.append(shared_runtime())

    threads = [threading.Thread(target=_call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.0)

    assert len(results) == 8
    assert len({id(r) for r in results}) == 1
