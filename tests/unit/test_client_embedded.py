"""Unit tests for `Rclone(execution="embedded")` construction and its first
ported method, `obscure()` (CLI-to-C-ABI migration ledger rows C01, M01).

Uses a fake `NativeBinding` wrapped in a real `RcloneRuntime`, injected
through the `runtime=` constructor parameter, so these tests exercise
`Rclone`'s embedded wiring without a built native library on disk. Real
`ctypes`/DLL behavior and CLI-vs-embedded parity are covered separately by
`tests/native/test_client_embedded_integration.py`.
"""

import json
import subprocess
from pathlib import Path

import pytest

from rclone_kit.client import Rclone
from rclone_kit.config import Config
from rclone_kit.exceptions import UnsupportedEmbeddedOperationError
from rclone_kit.native.runtime import RcloneRuntime
from rclone_kit.process import Process

_BUILD_INFO_JSON = json.dumps(
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
    """A fake `NativeBinding` driven by canned responses; see
    `tests/unit/test_native_runtime.py` for the same pattern.
    """

    def __init__(self) -> None:
        self.finalize_calls = 0
        self.rpc_calls: list[tuple[bytes, bytes]] = []
        self.last_initialize_payload: bytes = b"{}"
        self.next_rpc_response: tuple[int, bytes] = (200, b'{"obscured": "fake-obscured"}')

    def abi_version(self) -> int:
        return 1

    def build_info(self) -> tuple[int, bytes]:
        return (0, _BUILD_INFO_JSON)

    def initialize(self, payload: bytes) -> tuple[int, bytes]:
        self.last_initialize_payload = payload
        return (0, _BUILD_INFO_JSON)

    def rpc(self, method: bytes, payload: bytes) -> tuple[int, bytes]:
        self.rpc_calls.append((method, payload))
        return self.next_rpc_response

    def finalize(self) -> tuple[int, bytes]:
        self.finalize_calls += 1
        return (0, b"{}")


class FakeBackend:
    """A real `RcloneBackend`-shaped value, only used to prove that
    `execution="embedded"` rejects `backend=` before ever calling it.
    """

    def run(
        self,
        command: tuple[str, ...],
        *,
        check: bool = False,
        capture: bool | Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        raise NotImplementedError

    def launch(
        self,
        command: tuple[str, ...],
        *,
        capture: bool | None = None,
        log: Path | None = None,
    ) -> Process:
        raise NotImplementedError


def test_embedded_construction_initializes_runtime_with_default_config_path() -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)

    rclone = Rclone(None, execution="embedded", runtime=runtime)

    assert runtime.initialized
    assert json.loads(binding.last_initialize_payload) == {"configPath": None}
    rclone.close()


def test_embedded_construction_uses_explicit_path_directly(tmp_path) -> None:
    conf_path = tmp_path / "rclone.conf"
    conf_path.write_text("", encoding="utf-8")
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)

    rclone = Rclone(conf_path, execution="embedded", runtime=runtime)

    assert json.loads(binding.last_initialize_payload) == {"configPath": str(conf_path)}
    rclone.close()


def test_embedded_construction_materializes_config_value_once(tmp_path, monkeypatch) -> None:
    materialized_path = tmp_path / "materialized.conf"
    calls: list[None] = []

    def fake_make_temp_config_file():
        calls.append(None)
        return materialized_path

    monkeypatch.setattr("rclone_kit.client.make_temp_config_file", fake_make_temp_config_file)
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)
    config = Config("[remote]\ntype = memory\n")

    rclone = Rclone(config, execution="embedded", runtime=runtime)

    assert json.loads(binding.last_initialize_payload) == {"configPath": str(materialized_path)}
    assert len(calls) == 1
    rclone.close()


def test_obscure_uses_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.next_rpc_response = (200, b'{"obscured": "xyz"}')
    runtime = RcloneRuntime(binding)
    rclone = Rclone(None, execution="embedded", runtime=runtime)

    result = rclone.obscure("hunter2")

    assert result == "xyz"
    assert binding.rpc_calls[0][0] == b"core/obscure"
    assert json.loads(binding.rpc_calls[0][1]) == {"clear": "hunter2"}
    rclone.close()


def test_embedded_rejects_backend_kwarg() -> None:
    with pytest.raises(ValueError, match="CLI-only"):
        Rclone(None, execution="embedded", backend=FakeBackend())


def test_embedded_rejects_rclone_exe(tmp_path) -> None:
    with pytest.raises(ValueError, match="CLI-only"):
        Rclone(None, tmp_path / "rclone.exe", execution="embedded")


def test_embedded_rejects_runtime_and_library_path_together(tmp_path) -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)

    with pytest.raises(ValueError, match="at most one"):
        Rclone(None, execution="embedded", runtime=runtime, library_path=tmp_path / "lib.dll")


def test_cli_rejects_runtime_kwarg() -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)

    with pytest.raises(ValueError, match="execution='embedded'"):
        Rclone(None, execution="cli", runtime=runtime)


def test_close_only_closes_an_owned_runtime() -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)
    rclone = Rclone(None, execution="embedded", runtime=runtime)

    rclone.close()

    assert binding.finalize_calls == 0
    runtime.close()
    assert binding.finalize_calls == 1


def test_close_is_idempotent() -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)
    rclone = Rclone(None, execution="embedded", runtime=runtime)

    rclone.close()
    rclone.close()


def test_context_manager_closes_a_runtime_it_created_itself(tmp_path, monkeypatch) -> None:
    binding = FakeBinding()
    fake_library_path = tmp_path / "librclone_kit.dll"
    fake_library_path.touch()
    monkeypatch.setattr(
        "rclone_kit.client.resolve_library_path", lambda _explicit_path: fake_library_path
    )
    monkeypatch.setattr(
        RcloneRuntime,
        "from_library_path",
        staticmethod(lambda _library_path: RcloneRuntime(binding)),
    )

    with Rclone(None, execution="embedded", library_path=fake_library_path) as rclone:
        assert rclone.obscure("x") == "fake-obscured"

    assert binding.finalize_calls == 1


def test_run_raises_typed_error_when_backend_is_none() -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)
    rclone = Rclone(None, execution="embedded", runtime=runtime)

    with pytest.raises(UnsupportedEmbeddedOperationError):
        rclone._run(["listremotes"])

    rclone.close()


def test_launch_process_raises_typed_error_when_backend_is_none() -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)
    rclone = Rclone(None, execution="embedded", runtime=runtime)

    with pytest.raises(UnsupportedEmbeddedOperationError):
        rclone._launch_process(["rcd"])

    rclone.close()
