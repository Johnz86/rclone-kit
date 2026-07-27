"""Unit tests for `Rclone` construction and its dispatch to every embedded
operation.

Uses a fake `NativeBinding` wrapped in a real `RcloneRuntime`, injected
through the `runtime=` constructor parameter, so these tests exercise
`Rclone`'s embedded wiring without a built native library on disk. Real
`ctypes`/DLL behavior is covered separately by
`tests/native/test_client_embedded_integration.py`.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from rclone_kit.authorization import AuthorizationSession
from rclone_kit.client import (
    _COPY_DEFAULT_CHECKERS,
    _COPY_DEFAULT_LOW_LEVEL_RETRIES,
    _COPY_DEFAULT_RETRIES,
    _COPY_DEFAULT_TRANSFERS,
    Rclone,
)
from rclone_kit.config import Config
from rclone_kit.embedded_file_stream import EmbeddedFilesStream
from rclone_kit.exceptions import OperationFailedError, OperationShutdownError
from rclone_kit.job import _JobMonitor
from rclone_kit.native.runtime import RcloneRuntime
from rclone_kit.remote import Remote
from rclone_kit.serve_handle import ServeHandle

_COPY_SRC = "src:bucket"
_COPY_DST = "dst:bucket"

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


def test_embedded_construction_initializes_runtime_with_default_config_path() -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)

    rclone = Rclone(None, runtime=runtime)

    assert runtime.initialized
    assert json.loads(binding.last_initialize_payload) == {"configPath": None}
    rclone.close()


def test_embedded_construction_uses_explicit_path_directly(tmp_path) -> None:
    conf_path = tmp_path / "rclone.conf"
    conf_path.write_text("", encoding="utf-8")
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)

    rclone = Rclone(conf_path, runtime=runtime)

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

    rclone = Rclone(config, runtime=runtime)

    assert json.loads(binding.last_initialize_payload) == {"configPath": str(materialized_path)}
    assert len(calls) == 1
    rclone.close()


def test_obscure_uses_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.next_rpc_response = (200, b'{"obscured": "xyz"}')
    runtime = RcloneRuntime(binding)
    rclone = Rclone(None, runtime=runtime)

    result = rclone.obscure("hunter2")

    assert result == "xyz"
    assert binding.rpc_calls[0][0] == b"core/obscure"
    assert json.loads(binding.rpc_calls[0][1]) == {"clear": "hunter2"}
    rclone.close()


def test_embedded_rejects_runtime_and_library_path_together(tmp_path) -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)

    with pytest.raises(ValueError, match="at most one"):
        Rclone(None, runtime=runtime, library_path=tmp_path / "lib.dll")


def test_close_only_closes_an_owned_runtime() -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)
    rclone = Rclone(None, runtime=runtime)

    rclone.close()

    assert binding.finalize_calls == 0
    runtime.close()
    assert binding.finalize_calls == 1


def test_close_is_idempotent() -> None:
    binding = FakeBinding()
    runtime = RcloneRuntime(binding)
    rclone = Rclone(None, runtime=runtime)

    rclone.close()
    rclone.close()


def _client_owning_its_runtime(
    binding: FakeBinding, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Rclone:
    """Build an `Rclone` that genuinely owns its runtime, by making it load
    `binding` through the library-path path instead of accepting an
    injected `runtime` - the only way `close()` reaches
    `RcloneRuntime.close()` at all, and therefore the only way a test can
    observe whether the runtime was finalized or left open.
    """
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
    return Rclone(None, library_path=fake_library_path)


def test_context_manager_closes_a_runtime_it_created_itself(tmp_path, monkeypatch) -> None:
    binding = FakeBinding()

    with _client_owning_its_runtime(binding, tmp_path, monkeypatch) as rclone:
        assert rclone.obscure("x") == "fake-obscured"

    assert binding.finalize_calls == 1


def test_listremotes_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.rpc_responses_by_method[b"config/listremotes"] = (
        200,
        b'{"remotes": ["alpha"]}',
    )
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

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
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

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
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

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
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

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
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

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
    rclone = Rclone(config, runtime=RcloneRuntime(binding))

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
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    assert rclone.is_synced("src:bucket", "dst:bucket") is True
    rclone.close()


def test_diff_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    binding.rpc_responses_by_method[b"operations/check"] = (
        200,
        b'{"combined": ["= a.txt"]}',
    )
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    items = list(rclone.diff("src:bucket", "dst:bucket"))

    assert [i.path for i in items] == ["a.txt"]
    rclone.close()


def _finished_job_status_json(
    *, job_id: int = 1, execute_id: str = "exec-1", success: bool = True, error: str = ""
) -> bytes:
    return json.dumps(
        {
            "id": job_id,
            "executeId": execute_id,
            "group": "irrelevant",
            "duration": 0.01,
            "startTime": "2026-07-23T20:00:00Z",
            "endTime": "2026-07-23T20:00:01Z",
            "finished": True,
            "success": success,
            "error": error,
            "output": {},
        }
    ).encode("utf-8")


_STATS_JSON = json.dumps(
    {
        "bytes": 100,
        "totalBytes": 100,
        "checks": 0,
        "totalChecks": 0,
        "transfers": 1,
        "totalTransfers": 1,
        "errors": 0,
        "fatalError": False,
        "retryError": False,
        "speed": 0.0,
        "eta": None,
        "elapsedTime": 0.01,
    }
).encode("utf-8")


def _set_successful_job_responses(
    binding: FakeBinding, method: bytes, *, success: bool = True, error: str = ""
) -> None:
    """Wire up the async-job response chain (start -> job/status -> stats)
    any `_JobMonitor`-backed operation goes through, regardless of which RC
    method starts the job."""
    binding.rpc_responses_by_method[method] = (200, b'{"executeId": "exec-1", "jobid": 1}')
    binding.rpc_responses_by_method[b"job/status"] = (
        200,
        _finished_job_status_json(success=success, error=error),
    )
    binding.rpc_responses_by_method[b"core/stats"] = (200, _STATS_JSON)
    binding.rpc_responses_by_method[b"core/stats-delete"] = (200, b"{}")


def _set_successful_copy_responses(
    binding: FakeBinding, *, success: bool = True, error: str = ""
) -> None:
    _set_successful_job_responses(binding, b"rclonekit/copy", success=success, error=error)


def test_copy_to_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    _set_successful_job_responses(binding, b"operations/copyfile")
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    result = rclone.copy_to("remote:a.txt", "remote:b.txt")

    assert result.ok is True
    assert binding.rpc_calls[0][0] == b"operations/copyfile"
    rclone.close()


def test_copy_to_raises_operation_failed_error_by_default_on_failure() -> None:
    binding = FakeBinding()
    _set_successful_job_responses(binding, b"operations/copyfile", success=False, error="boom")
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    with pytest.raises(OperationFailedError):
        rclone.copy_to("remote:a.txt", "remote:b.txt")
    rclone.close()


def test_purge_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    _set_successful_job_responses(binding, b"operations/purge")
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    result = rclone.purge("remote:path/to/dir")

    assert result.ok is True
    assert binding.rpc_calls[0][0] == b"operations/purge"
    rclone.close()


def test_purge_never_raises_on_failure_when_embedded() -> None:
    binding = FakeBinding()
    _set_successful_job_responses(binding, b"operations/purge", success=False, error="boom")
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    result = rclone.purge("remote:path/to/dir")

    assert result.ok is False
    rclone.close()


def test_cleanup_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    _set_successful_job_responses(binding, b"operations/cleanup")
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    result = rclone.cleanup("remote:")

    assert result.ok is True
    assert binding.rpc_calls[0][0] == b"operations/cleanup"
    rclone.close()


def test_cleanup_never_raises_on_failure_when_embedded() -> None:
    binding = FakeBinding()
    _set_successful_job_responses(binding, b"operations/cleanup", success=False, error="boom")
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    result = rclone.cleanup("remote:")

    assert result.ok is False
    rclone.close()


def test_start_copy_dispatches_to_rclonekit_copy_and_returns_a_job_handle() -> None:
    binding = FakeBinding()
    _set_successful_copy_responses(binding)
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    handle = rclone.start_copy("src:bucket", "dst:bucket")
    result = handle.wait(timeout=5.0)

    assert result.ok is True
    assert binding.rpc_calls[0][0] == b"rclonekit/copy"
    rclone.close()


def test_copy_dispatches_to_start_copy_with_its_tuned_defaults() -> None:
    binding = FakeBinding()
    _set_successful_copy_responses(binding)
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    result = rclone.copy("src:bucket", "dst:bucket")

    assert result.ok is True
    request = json.loads(binding.rpc_calls[0][1])
    assert request["_config"] == {
        "Checkers": 1000,
        "Transfers": 32,
        "LowLevelRetries": 10,
        "Retries": 3,
    }
    rclone.close()


@dataclass(frozen=True)
class ExplicitZeroTuningCase:
    field_name: str
    copy_with_zero: Callable[[Rclone], object]


COPY_TRANSFERS_ZERO = ExplicitZeroTuningCase(
    "transfers", lambda rclone: rclone.copy(_COPY_SRC, _COPY_DST, transfers=0)
)
COPY_CHECKERS_ZERO = ExplicitZeroTuningCase(
    "checkers", lambda rclone: rclone.copy(_COPY_SRC, _COPY_DST, checkers=0)
)
COPY_LOW_LEVEL_RETRIES_ZERO = ExplicitZeroTuningCase(
    "low_level_retries", lambda rclone: rclone.copy(_COPY_SRC, _COPY_DST, low_level_retries=0)
)
COPY_RETRIES_ZERO = ExplicitZeroTuningCase(
    "retries", lambda rclone: rclone.copy(_COPY_SRC, _COPY_DST, retries=0)
)

EXPLICIT_ZERO_TUNING_CASES = [
    COPY_TRANSFERS_ZERO,
    COPY_CHECKERS_ZERO,
    COPY_LOW_LEVEL_RETRIES_ZERO,
    COPY_RETRIES_ZERO,
]


@pytest.mark.parametrize(
    "case",
    EXPLICIT_ZERO_TUNING_CASES,
    ids=[
        "copy_transfers_zero",
        "copy_checkers_zero",
        "copy_low_level_retries_zero",
        "copy_retries_zero",
    ],
)
def test_copy_passes_an_explicit_zero_through_instead_of_its_tuned_default(
    case: ExplicitZeroTuningCase,
) -> None:
    """An explicit `0` must reach `TransferOptions` and be rejected there,
    rather than being read as "unset" and silently rewritten to `copy()`'s
    tuned default - which would run a transfer the caller never asked for.
    """
    binding = FakeBinding()
    _set_successful_copy_responses(binding)
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    with pytest.raises(ValueError, match=f"{case.field_name} must be a positive integer"):
        case.copy_with_zero(rclone)

    assert binding.rpc_calls == []
    rclone.close()


def test_copy_forwards_an_explicit_multi_thread_streams_zero_to_the_rc_config() -> None:
    """`0` is rclone's documented "disable multi-thread transfers" value,
    so it must survive the whole `copy()` -> `TransferOptions` -> `_config`
    chain alongside the tuned defaults.
    """
    binding = FakeBinding()
    _set_successful_copy_responses(binding)
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    result = rclone.copy(_COPY_SRC, _COPY_DST, multi_thread_streams=0)

    assert result.ok is True
    request = json.loads(binding.rpc_calls[0][1])
    assert request["_config"] == {
        "Checkers": _COPY_DEFAULT_CHECKERS,
        "Transfers": _COPY_DEFAULT_TRANSFERS,
        "LowLevelRetries": _COPY_DEFAULT_LOW_LEVEL_RETRIES,
        "Retries": _COPY_DEFAULT_RETRIES,
        "MultiThreadStreams": 0,
        "MultiThreadSet": True,
    }
    rclone.close()


def test_copy_dir_dispatches_to_start_copy_without_copys_tuned_defaults() -> None:
    binding = FakeBinding()
    _set_successful_copy_responses(binding)
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    result = rclone.copy_dir("src:bucket", "dst:bucket")

    assert result.ok is True
    request = json.loads(binding.rpc_calls[0][1])
    assert request.get("_config", {}) == {}
    rclone.close()


def test_copy_dir_does_not_raise_on_failure() -> None:
    binding = FakeBinding()
    _set_successful_copy_responses(binding, success=False, error="boom")
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    result = rclone.copy_dir("src:bucket", "dst:bucket")

    assert result.ok is False
    rclone.close()


def test_copy_remote_dispatches_to_start_copy() -> None:
    binding = FakeBinding()
    _set_successful_copy_responses(binding)
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    result = rclone.copy_remote(Remote("src", rclone), Remote("dst", rclone))

    assert result.ok is True
    rclone.close()


def _set_serve_responses(binding: FakeBinding, *, serve_id: str = "http-1") -> None:
    binding.rpc_responses_by_method[b"serve/start"] = (
        200,
        json.dumps({"id": serve_id, "addr": "127.0.0.1:54321"}).encode("utf-8"),
    )
    binding.rpc_responses_by_method[b"serve/stop"] = (200, b"{}")


def test_serve_http_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    _set_serve_responses(binding)
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    server = rclone.serve_http("remote:base")

    assert server.url == "http://127.0.0.1:54321"
    assert binding.rpc_calls[0][0] == b"serve/start"
    server.shutdown()
    rclone.close()


def test_serve_webdav_dispatches_to_rc_client_when_embedded() -> None:
    binding = FakeBinding()
    _set_serve_responses(binding, serve_id="webdav-1")
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    handle = rclone.serve_webdav("remote:base", "alice", "hunter2", addr="127.0.0.1:0")
    assert isinstance(handle, ServeHandle)

    assert handle.id == "webdav-1"
    assert handle.addr == "127.0.0.1:54321"
    assert binding.rpc_calls[0][0] == b"serve/start"
    handle.dispose()
    rclone.close()


def test_close_disposes_serve_handles_the_caller_never_disposed() -> None:
    binding = FakeBinding()
    _set_serve_responses(binding)
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    handle = rclone.serve_webdav("remote:base", "alice", "hunter2", addr="127.0.0.1:0")
    assert isinstance(handle, ServeHandle)
    assert handle.closed is False

    rclone.close()

    assert handle.closed is True
    assert binding.rpc_calls[-1][0] == b"serve/stop"


def test_disposing_a_serve_handle_removes_it_from_the_client_tracking_set() -> None:
    # A client that starts and disposes many short-lived serve sessions
    # over its lifetime must not leak one tracked entry per session.
    binding = FakeBinding()
    _set_serve_responses(binding)
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    handle = rclone.serve_webdav("remote:base", "alice", "hunter2", addr="127.0.0.1:0")
    assert handle in rclone._serve_handles

    handle.dispose()

    assert handle not in rclone._serve_handles
    rclone.close()


def test_close_does_not_double_stop_an_already_disposed_serve_handle() -> None:
    binding = FakeBinding()
    _set_serve_responses(binding)
    rclone = Rclone(None, runtime=RcloneRuntime(binding))

    handle = rclone.serve_webdav("remote:base", "alice", "hunter2", addr="127.0.0.1:0")
    assert isinstance(handle, ServeHandle)
    handle.dispose()
    stop_calls_after_manual_dispose = sum(1 for m, _ in binding.rpc_calls if m == b"serve/stop")

    rclone.close()

    stop_calls_after_close = sum(1 for m, _ in binding.rpc_calls if m == b"serve/stop")
    assert stop_calls_after_close == stop_calls_after_manual_dispose == 1


_RESOURCE_FAILURE_MESSAGE = "simulated teardown failure"


class _RaisingResource:
    """A tracked resource whose close raises, standing in for an
    `EmbeddedFilesStream` or `AuthorizationSession` that fails during
    teardown - unlike `ServeHandle`/`MountHandle`, neither of those
    swallows its own failures.
    """

    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError(_RESOURCE_FAILURE_MESSAGE)


class _FakeJobMonitor:
    """Stands in for `_JobMonitor` so a test can choose the shutdown
    outcome directly, instead of arranging a real job that refuses to
    settle within the deadline.
    """

    def __init__(self, *, all_settled: bool) -> None:
        self._all_settled = all_settled
        self.shutdown_calls = 0

    def shutdown(self, *, deadline_seconds: float) -> bool:
        assert deadline_seconds > 0
        self.shutdown_calls += 1
        return self._all_settled


def test_close_isolates_failing_resources_from_every_other_cleanup_step(
    tmp_path, monkeypatch
) -> None:
    binding = FakeBinding()
    _set_serve_responses(binding)
    rclone = _client_owning_its_runtime(binding, tmp_path, monkeypatch)
    monitor = _FakeJobMonitor(all_settled=True)
    rclone._job_monitor = cast(_JobMonitor, monitor)
    serve_handle = rclone.serve_webdav("remote:base", "alice", "hunter2", addr="127.0.0.1:0")
    stream = _RaisingResource()
    rclone._file_streams.add(cast(EmbeddedFilesStream, stream))
    session = _RaisingResource()
    rclone._authorization_sessions.add(cast(AuthorizationSession, session))

    with pytest.raises(ExceptionGroup) as raised:
        rclone.close()

    assert [str(error) for error in raised.value.exceptions] == [_RESOURCE_FAILURE_MESSAGE] * 2
    assert stream.close_calls == 1
    assert session.close_calls == 1
    assert serve_handle.closed is True
    assert monitor.shutdown_calls == 1
    assert binding.finalize_calls == 1


def test_close_still_reports_unsettled_jobs_and_leaves_the_runtime_open(
    tmp_path, monkeypatch
) -> None:
    binding = FakeBinding()
    rclone = _client_owning_its_runtime(binding, tmp_path, monkeypatch)
    monitor = _FakeJobMonitor(all_settled=False)
    rclone._job_monitor = cast(_JobMonitor, monitor)
    stream = _RaisingResource()
    rclone._file_streams.add(cast(EmbeddedFilesStream, stream))

    with pytest.raises(OperationShutdownError):
        rclone.close()

    assert stream.close_calls == 1
    assert binding.finalize_calls == 0
