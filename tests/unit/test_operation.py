"""Unit tests for `rclone_kit.operation`'s public value types.

These are parser-independent: no RC JSON, no `RcJobClient`, no native
runtime. They only prove the value types' own invariants - immutability,
timezone-awareness, terminal-state/`ended_at` consistency, and
`OperationResult`'s `ok`/`error`/`cancelled` consistency rules. Wire
parsing (RC JSON -> these types) is a separate, later concern (`rc/jobs.py`).
"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from rclone_kit.operation import (
    ActiveTransfer,
    JobState,
    JobStatus,
    OperationAttempt,
    OperationResult,
    OperationWarning,
    TransferStats,
)

_NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 7, 23, 12, 0, 5, tzinfo=UTC)


def _job_status(**overrides: object) -> JobStatus:
    fields: dict[str, object] = {
        "job_id": 1,
        "execute_id": "exec-1",
        "group": "rclone-kit/client-1/op-1",
        "state": JobState.RUNNING,
        "started_at": _NOW,
        "ended_at": None,
        "duration": 0.0,
        "error": None,
    }
    fields.update(overrides)
    return JobStatus(**fields)  # type: ignore[arg-type]


def _operation_result(**overrides: object) -> OperationResult:
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


class TestJobState:
    def test_terminal_states(self) -> None:
        for state in (
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.LOST,
        ):
            assert state.is_terminal

    def test_non_terminal_states(self) -> None:
        for state in (JobState.RUNNING, JobState.CANCELLATION_REQUESTED):
            assert not state.is_terminal


class TestJobStatus:
    def test_is_frozen(self) -> None:
        status = _job_status()
        with pytest.raises(FrozenInstanceError):
            status.job_id = 2  # type: ignore[misc]

    def test_output_defaults_to_empty_and_is_immutable(self) -> None:
        status = _job_status()
        assert dict(status.output) == {}
        with pytest.raises(TypeError):
            status.output["x"] = 1  # type: ignore[index]

    def test_mutating_the_original_dict_after_construction_does_not_leak_in(self) -> None:
        source = {"a": 1}
        status = _job_status(output=source)
        source["a"] = 2
        assert status.output["a"] == 1

    def test_naive_started_at_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _job_status(started_at=datetime(2026, 7, 23, 12, 0, 0))  # noqa: DTZ001

    def test_naive_ended_at_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _job_status(
                state=JobState.SUCCEEDED,
                ended_at=datetime(2026, 7, 23, 12, 0, 5),  # noqa: DTZ001
            )

    def test_negative_duration_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            _job_status(duration=-1.0)

    def test_terminal_state_requires_ended_at(self) -> None:
        with pytest.raises(ValueError, match="requires ended_at"):
            _job_status(state=JobState.SUCCEEDED, ended_at=None)

    def test_non_terminal_state_forbids_ended_at(self) -> None:
        with pytest.raises(ValueError, match="must not have ended_at"):
            _job_status(state=JobState.RUNNING, ended_at=_LATER)

    def test_terminal_state_with_ended_at_is_valid(self) -> None:
        status = _job_status(state=JobState.FAILED, ended_at=_LATER, error="boom")
        assert status.state is JobState.FAILED
        assert status.ended_at == _LATER


class TestActiveTransfer:
    def test_valid_construction(self) -> None:
        transfer = ActiveTransfer(
            name="a.txt", bytes=50, size=100, speed=10.0, percentage=50, eta_seconds=5.0
        )
        assert transfer.percentage == 50

    def test_negative_bytes_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            ActiveTransfer(
                name="a.txt", bytes=-1, size=100, speed=1.0, percentage=None, eta_seconds=None
            )

    def test_percentage_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"0\.\.100"):
            ActiveTransfer(
                name="a.txt", bytes=0, size=100, speed=1.0, percentage=101, eta_seconds=None
            )

    def test_bool_is_not_accepted_as_bytes(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            ActiveTransfer(
                name="a.txt", bytes=True, size=100, speed=1.0, percentage=None, eta_seconds=None
            )  # type: ignore[arg-type]


class TestTransferStats:
    def _stats(self, **overrides: object) -> TransferStats:
        fields: dict[str, object] = {
            "bytes": 0,
            "total_bytes": 0,
            "checks": 0,
            "total_checks": 0,
            "transfers": 0,
            "total_transfers": 0,
            "errors": 0,
            "fatal_error": False,
            "retry_error": False,
            "speed": 0.0,
            "eta_seconds": None,
            "elapsed_seconds": 0.0,
        }
        fields.update(overrides)
        return TransferStats(**fields)  # type: ignore[arg-type]

    def test_is_frozen(self) -> None:
        stats = self._stats()
        with pytest.raises(FrozenInstanceError):
            stats.bytes = 1  # type: ignore[misc]

    def test_defaults_for_active_arrays(self) -> None:
        stats = self._stats()
        assert stats.active_transfers == ()
        assert stats.active_checks == ()

    def test_negative_errors_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            self._stats(errors=-1)


class TestOperationAttempt:
    def _attempt(self, **overrides: object) -> OperationAttempt:
        fields: dict[str, object] = {
            "number": 1,
            "started_at": _NOW,
            "ended_at": _LATER,
            "duration": 5.0,
            "ok": True,
            "error": None,
            "fatal_error": False,
            "retry_error": False,
        }
        fields.update(overrides)
        return OperationAttempt(**fields)  # type: ignore[arg-type]

    def test_number_must_be_at_least_one(self) -> None:
        with pytest.raises(ValueError, match="number"):
            self._attempt(number=0)

    def test_ended_before_started_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="ended_at must not precede"):
            self._attempt(started_at=_LATER, ended_at=_NOW)


class TestOperationWarning:
    def test_is_frozen_and_detail_immutable(self) -> None:
        warning = OperationWarning(message="slow backend", detail={"remote": "s3:"})
        with pytest.raises(FrozenInstanceError):
            warning.message = "x"  # type: ignore[misc]
        with pytest.raises(TypeError):
            warning.detail["x"] = 1  # type: ignore[index]


class TestOperationResult:
    def test_is_frozen(self) -> None:
        result = _operation_result()
        with pytest.raises(FrozenInstanceError):
            result.ok = False  # type: ignore[misc]

    def test_ended_before_started_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="ended_at must not precede"):
            _operation_result(started_at=_LATER, ended_at=_NOW)

    def test_ok_true_with_error_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="ok=True cannot carry"):
            _operation_result(ok=True, error="boom")

    def test_ok_false_requires_error_or_cancelled(self) -> None:
        with pytest.raises(ValueError, match="requires either error or cancelled"):
            _operation_result(ok=False, error=None, cancelled=False)

    def test_ok_false_with_error_is_valid(self) -> None:
        result = _operation_result(ok=False, error="boom")
        assert result.error == "boom"

    def test_ok_false_with_cancelled_and_no_error_is_valid(self) -> None:
        result = _operation_result(ok=False, error=None, cancelled=True)
        assert result.cancelled

    def test_cancelled_and_ok_true_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot also be ok=True"):
            _operation_result(ok=True, cancelled=True, error=None)

    def test_job_ids_and_attempts_accept_multiple_entries(self) -> None:
        attempt_1 = OperationAttempt(
            number=1,
            started_at=_NOW,
            ended_at=_LATER,
            duration=5.0,
            ok=False,
            error="transient",
            fatal_error=False,
            retry_error=True,
        )
        attempt_2 = OperationAttempt(
            number=2,
            started_at=_LATER,
            ended_at=_LATER,
            duration=0.0,
            ok=True,
            error=None,
            fatal_error=False,
            retry_error=False,
        )
        result = _operation_result(job_ids=(1, 2), attempts=(attempt_1, attempt_2))
        assert result.job_ids == (1, 2)
        assert len(result.attempts) == 2
