"""Unit tests for `CompletedProcess.from_operation_result()`, the
compatibility boundary between the new execution-independent
`OperationResult` and the pre-existing subprocess-shaped `CompletedProcess`
public return type (CLI-to-C-ABI migration Wave D design, section 5.6).

`from_subprocess()`'s existing CLI-backed behavior is already exercised
throughout the rest of the test suite (e.g. `test_transfer_ops.py`); this
file only covers the new operation-result-backed construction path.
"""

from datetime import UTC, datetime

from rclone_kit.completed_process import CompletedProcess
from rclone_kit.operation import OperationResult

_NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 7, 23, 12, 0, 5, tzinfo=UTC)


def _result(**overrides: object) -> OperationResult:
    fields: dict[str, object] = {
        "ok": True,
        "operation": "copy",
        "source": "src:",
        "destination": "dst:",
        "job_ids": (1,),
        "stats": None,
        "warnings": (),
        "attempts": (),
        "started_at": _NOW,
        "ended_at": _LATER,
        "duration": 5.0,
        "cancelled": False,
        "error": None,
    }
    fields.update(overrides)
    return OperationResult(**fields)  # type: ignore[arg-type]


def test_from_operation_result_exposes_the_result() -> None:
    result = _result()
    completed = CompletedProcess.from_operation_result(result)
    assert completed.operation_result is result


def test_from_operation_result_ok_delegates_to_the_result() -> None:
    assert CompletedProcess.from_operation_result(_result(ok=True)).ok is True

    failed = _result(ok=False, error="boom")
    assert CompletedProcess.from_operation_result(failed).ok is False


def test_from_operation_result_returncode_is_synthetic() -> None:
    assert CompletedProcess.from_operation_result(_result(ok=True)).returncode == 0

    failed = _result(ok=False, error="boom")
    assert CompletedProcess.from_operation_result(failed).returncode == 1


def test_from_operation_result_completed_list_is_empty() -> None:
    completed = CompletedProcess.from_operation_result(_result())
    assert completed.completed == []


def test_from_operation_result_stdout_and_stderr_are_empty_not_invented() -> None:
    completed = CompletedProcess.from_operation_result(_result())
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_from_operation_result_failed_and_successes_stay_empty() -> None:
    completed = CompletedProcess.from_operation_result(_result(ok=False, error="boom"))
    assert completed.failed() == []
    assert completed.successes() == []


def test_str_mentions_the_wrapped_result_not_a_fake_command() -> None:
    result = _result(operation="cleanup")
    text = str(CompletedProcess.from_operation_result(result))
    assert "cleanup" in text


def test_from_subprocess_instances_have_no_operation_result() -> None:
    import subprocess

    completed = CompletedProcess.from_subprocess(
        subprocess.CompletedProcess(args=["rclone"], returncode=0, stdout="", stderr="")
    )
    assert completed.operation_result is None
