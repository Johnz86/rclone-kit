"""Unit tests for `rclone_kit.process.Process`'s resource-ownership lifecycle.

`_spawn_bytes_mode` is replaced with a fake so no real process is spawned;
these tests exist to prove that `Process` tracks itself in
`process._LIVE_PROCESSES` for as long as it is live, discards itself on
`dispose()` (idempotently), never registers a per-instance `atexit`
callback, and that `_cleanup_live_processes` only terminates processes still
running at interpreter exit.
"""

from pathlib import Path

import pytest

from rclone_kit import process as process_module
from rclone_kit.process import Process, ProcessArgs


class _FakePopen:
    def __init__(self) -> None:
        self.pid = 9999
        self._exited = False

    def poll(self) -> int | None:
        return 0 if self._exited else None

    def wait(self) -> int:
        self._exited = True
        return 0

    @property
    def stdout(self) -> None:
        return None

    @property
    def stderr(self) -> None:
        return None

    def send_signal(self, _sig: int) -> None:
        return None


def _make_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Process:
    monkeypatch.setattr(process_module, "_spawn_bytes_mode", lambda _cmd, _kwargs: _FakePopen())
    exe = tmp_path / "rclone"
    exe.touch()
    return Process(ProcessArgs(cmd=[], rclone_conf=None, rclone_exe=exe, cmd_list=["version"]))


def test_process_registers_itself_and_dispose_discards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(process_module, "terminate_process_tree", lambda _pid: None)
    proc = _make_process(monkeypatch, tmp_path)

    assert proc in process_module._LIVE_PROCESSES

    proc.dispose()

    assert proc not in process_module._LIVE_PROCESSES


def test_process_dispose_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(process_module, "terminate_process_tree", lambda _pid: None)
    proc = _make_process(monkeypatch, tmp_path)

    proc.dispose()
    proc.dispose()

    assert proc not in process_module._LIVE_PROCESSES


def test_process_does_not_register_atexit_per_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    register_calls: list[object] = []
    monkeypatch.setattr(process_module.atexit, "register", register_calls.append)

    _make_process(monkeypatch, tmp_path)
    _make_process(monkeypatch, tmp_path)

    assert register_calls == []


def test_cleanup_live_processes_terminates_only_running_processes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    terminated_pids: list[int] = []
    monkeypatch.setattr(process_module, "terminate_process_tree", terminated_pids.append)

    running = _make_process(monkeypatch, tmp_path)
    finished = _make_process(monkeypatch, tmp_path)
    finished.process.wait()

    process_module._cleanup_live_processes()

    assert terminated_pids == [running.process.pid]

    running.dispose()
    finished.dispose()
