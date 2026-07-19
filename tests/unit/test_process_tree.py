"""Unit tests for `rclone_kit.process_tree`.

Every test fakes the `psutil` boundary (`psutil.Process`, `psutil.wait_procs`)
so no real process is spawned or terminated.
"""

from dataclasses import dataclass, field

import psutil
import pytest

from rclone_kit import process_tree
from rclone_kit.process_tree import terminate_process_tree


@dataclass
class FakeProcess:
    pid: int
    running: bool = True
    child_processes: list["FakeProcess"] = field(default_factory=list)
    terminate_calls: int = 0
    kill_calls: int = 0

    def children(self, recursive: bool = True) -> list["FakeProcess"]:
        return self.child_processes

    def is_running(self) -> bool:
        return self.running

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self.running = False


def _fake_wait_procs_all_gone(
    processes: list[FakeProcess],
    timeout: float,  # noqa: ARG001 -- keyword name must match psutil.wait_procs
) -> tuple[list[FakeProcess], list[FakeProcess]]:
    return list(processes), []


def _fake_wait_procs_all_alive(
    processes: list[FakeProcess],
    timeout: float,  # noqa: ARG001 -- keyword name must match psutil.wait_procs
) -> tuple[list[FakeProcess], list[FakeProcess]]:
    return [], list(processes)


def test_terminates_children_before_parent_and_does_not_escalate_on_quick_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = FakeProcess(pid=2)
    parent = FakeProcess(pid=1, child_processes=[child])
    monkeypatch.setattr(process_tree.psutil, "Process", lambda _pid: parent)
    monkeypatch.setattr(process_tree.psutil, "wait_procs", _fake_wait_procs_all_gone)

    terminate_process_tree(1)

    assert child.terminate_calls == 1
    assert parent.terminate_calls == 1
    assert child.kill_calls == 0
    assert parent.kill_calls == 0


def test_escalates_to_kill_when_processes_do_not_exit_in_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = FakeProcess(pid=2)
    parent = FakeProcess(pid=1, child_processes=[child])
    monkeypatch.setattr(process_tree.psutil, "Process", lambda _pid: parent)
    monkeypatch.setattr(process_tree.psutil, "wait_procs", _fake_wait_procs_all_alive)

    terminate_process_tree(1)

    assert child.kill_calls == 1
    assert parent.kill_calls == 1


def test_missing_process_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_no_such_process(pid: int) -> FakeProcess:
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(process_tree.psutil, "Process", raise_no_such_process)

    terminate_process_tree(999999)


def test_already_stopped_parent_with_no_children_never_calls_wait_procs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = FakeProcess(pid=1, running=False)
    monkeypatch.setattr(process_tree.psutil, "Process", lambda _pid: parent)

    def fail_if_called(
        _processes: list[FakeProcess],
        timeout: float,  # noqa: ARG001 -- keyword name must match psutil.wait_procs
    ) -> tuple[list[FakeProcess], list[FakeProcess]]:
        raise AssertionError("wait_procs must not run when nothing needs to be terminated")

    monkeypatch.setattr(process_tree.psutil, "wait_procs", fail_if_called)

    terminate_process_tree(1)

    assert parent.terminate_calls == 0
