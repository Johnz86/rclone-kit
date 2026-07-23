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
        self.rpc_responses_by_method: dict[bytes, tuple[int, bytes]] = {}

    def abi_version(self) -> int:
        return 1

    def build_info(self) -> tuple[int, bytes]:
        return (0, _BUILD_INFO_JSON)

    def initialize(self, payload: bytes) -> tuple[int, bytes]:
        self.last_initialize_payload = payload
        return (0, _BUILD_INFO_JSON)

    def rpc(self, method: bytes, payload: bytes) -> tuple[int, bytes]:
        self.rpc_calls.append((method, payload))
        return self.rpc_responses_by_method.get(method, self.next_rpc_response)

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


def test_listremotes_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.rpc_responses_by_method[b"config/listremotes"] = (
        200,
        b'{"remotes": ["alpha"]}',
    )
    rclone = Rclone(None, execution="embedded", runtime=RcloneRuntime(binding))

    remotes = rclone.listremotes()

    assert [r.name for r in remotes] == ["alpha"]
    rclone.close()


def test_stat_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.rpc_responses_by_method[b"operations/stat"] = (
        200,
        json.dumps(
            {
                "item": {
                    "Path": "object.txt",
                    "Name": "object.txt",
                    "Size": 3,
                    "MimeType": "text/plain",
                    "ModTime": "2024-01-01T00:00:00Z",
                    "IsDir": False,
                }
            }
        ).encode("utf-8"),
    )
    rclone = Rclone(None, execution="embedded", runtime=RcloneRuntime(binding))

    file = rclone.stat("remote:object.txt")

    assert file.name == "object.txt"
    assert (
        rclone.modtime(  # transitive from stat(): no embedded branch needed
            "remote:object.txt"
        )
        == "2024-01-01T00:00:00Z"
    )
    rclone.close()


def test_exists_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.rpc_responses_by_method[b"operations/stat"] = (200, b'{"item": null}')
    rclone = Rclone(None, execution="embedded", runtime=RcloneRuntime(binding))

    assert rclone.exists("remote:missing.txt") is False
    rclone.close()


def test_size_file_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.rpc_responses_by_method[b"operations/stat"] = (
        200,
        json.dumps(
            {
                "item": {
                    "Path": "object.txt",
                    "Name": "object.txt",
                    "Size": 42,
                    "MimeType": "text/plain",
                    "ModTime": "2024-01-01T00:00:00Z",
                    "IsDir": False,
                }
            }
        ).encode("utf-8"),
    )
    rclone = Rclone(None, execution="embedded", runtime=RcloneRuntime(binding))

    assert rclone.size_file("remote:object.txt").as_int() == 42
    rclone.close()


def test_config_paths_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.rpc_responses_by_method[b"config/paths"] = (
        200,
        json.dumps({"config": "/x/rclone.conf", "cache": "/x/cache", "temp": "/x/tmp"}).encode(
            "utf-8"
        ),
    )
    rclone = Rclone(None, execution="embedded", runtime=RcloneRuntime(binding))

    paths = rclone.config_paths()

    assert [p.name for p in paths] == ["rclone.conf", "cache", "tmp"]
    rclone.close()


def test_is_s3_and_get_s3_credentials_work_unmodified_under_embedded_execution() -> None:
    """M05/M06 need no embedded adapter at all: both already operate only on
    `self.config`, never on `self._backend`/`self._rc_client`, so they work
    identically regardless of execution mode. Asserts no RC call happens.
    """
    binding = FakeBinding()
    config = Config(
        "[myremote]\n"
        "type = s3\n"
        "provider = AWS\n"
        "access_key_id = AKIAEXAMPLE\n"
        "secret_access_key = secretexample\n"
    )
    rclone = Rclone(config, execution="embedded", runtime=RcloneRuntime(binding))

    assert rclone.is_s3("myremote:mybucket/key.txt") is True
    creds = rclone.get_s3_credentials("myremote:mybucket/key.txt")

    assert creds.access_key_id == "AKIAEXAMPLE"
    assert binding.rpc_calls == []
    rclone.close()


def test_is_synced_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.rpc_responses_by_method[b"operations/check"] = (
        200,
        b'{"success": true, "status": "OK"}',
    )
    rclone = Rclone(None, execution="embedded", runtime=RcloneRuntime(binding))

    assert rclone.is_synced("src:bucket", "dst:bucket") is True
    rclone.close()


def test_diff_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.rpc_responses_by_method[b"operations/check"] = (
        200,
        b'{"combined": ["= a.txt"]}',
    )
    rclone = Rclone(None, execution="embedded", runtime=RcloneRuntime(binding))

    items = list(rclone.diff("src:bucket", "dst:bucket"))

    assert [i.path for i in items] == ["a.txt"]
    rclone.close()


def test_copy_to_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.rpc_responses_by_method[b"operations/copyfile"] = (200, b"{}")
    rclone = Rclone(None, execution="embedded", runtime=RcloneRuntime(binding))

    result = rclone.copy_to("remote:a.txt", "remote:b.txt")

    assert result.ok is True
    assert binding.rpc_calls[0][0] == b"operations/copyfile"
    rclone.close()


def test_purge_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.rpc_responses_by_method[b"operations/purge"] = (200, b"{}")
    rclone = Rclone(None, execution="embedded", runtime=RcloneRuntime(binding))

    result = rclone.purge("remote:path/to/dir")

    assert result.ok is True
    assert binding.rpc_calls[0][0] == b"operations/purge"
    rclone.close()


def test_cleanup_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.rpc_responses_by_method[b"operations/cleanup"] = (200, b"{}")
    rclone = Rclone(None, execution="embedded", runtime=RcloneRuntime(binding))

    result = rclone.cleanup("remote:")

    assert result.ok is True
    assert binding.rpc_calls[0][0] == b"operations/cleanup"
    rclone.close()
