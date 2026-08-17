"""Public, execution-independent operation result/status value types.

`JobHandle` (the RC job boundary) produces these types, never raw RC JSON
or ctypes structures - callers never see a process-shaped value.

This module is a leaf: it imports no native, RC, client, or subprocess
modules, and depends on nothing else in this package. Wire parsing (the RC
JSON -> these types boundary) belongs in `rc/jobs.py`, not here - these are
domain objects, not a wire schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def _require_aware(name: str, value: datetime | None) -> None:
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime, got a naive one: {value!r}")


def _require_non_negative(name: str, value: float) -> None:
    if isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative number, got {value!r}")


def _require_non_negative_int(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative int, got {value!r}")


_PERCENTAGE_MAX = 100


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    if isinstance(value, MappingProxyType):
        return value
    return MappingProxyType(dict(value))


class JobState(Enum):
    """The lifecycle state of one rclone RC job, as tracked by rclone-kit.

    Rclone's own `job/status` reports only `finished`/`success`/`error`.
    `CANCELLATION_REQUESTED` and `CANCELLED` are added from state this
    client itself tracks: `CANCELLED` is only ever used after *this*
    client requested cancellation and the terminal status is consistent
    with it - an unrelated failure that merely races a cancel request is
    `FAILED`, not `CANCELLED`. `LOST` means rclone's job-expiry window
    elapsed before any terminal state could be observed.
    """

    RUNNING = "running"
    CANCELLATION_REQUESTED = "cancellation_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_JOB_STATES


_TERMINAL_JOB_STATES = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.LOST}
)


@dataclass(frozen=True)
class JobStatus:
    """One immutable snapshot of an rclone RC job's status.

    `job_id` alone is not a stable identity across rclone process restarts
    (job IDs restart from one); `execute_id` disambiguates it. A consumer
    that receives a status for an unexpected `execute_id` has a job-identity
    error, not an ordinary status update.
    """

    job_id: int
    execute_id: str
    group: str
    state: JobState
    started_at: datetime
    ended_at: datetime | None
    duration: float
    error: str | None
    output: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_aware("started_at", self.started_at)
        _require_aware("ended_at", self.ended_at)
        _require_non_negative("duration", self.duration)
        object.__setattr__(self, "output", _freeze_mapping(self.output))
        if self.state.is_terminal and self.ended_at is None:
            raise ValueError(f"terminal state {self.state!r} requires ended_at to be set")
        if not self.state.is_terminal and self.ended_at is not None:
            raise ValueError(f"non-terminal state {self.state!r} must not have ended_at set")


@dataclass(frozen=True)
class ActiveTransfer:
    """One in-progress file transfer, as reported by `core/stats`'
    `transferring` array."""

    name: str
    bytes: int
    size: int
    speed: float
    percentage: int | None
    eta_seconds: float | None

    def __post_init__(self) -> None:
        _require_non_negative_int("bytes", self.bytes)
        _require_non_negative_int("size", self.size)
        _require_non_negative("speed", self.speed)
        if self.percentage is not None and not (0 <= self.percentage <= _PERCENTAGE_MAX):
            raise ValueError(f"percentage must be within 0..100, got {self.percentage!r}")
        if self.eta_seconds is not None:
            _require_non_negative("eta_seconds", self.eta_seconds)


@dataclass(frozen=True)
class TransferStats:
    """A snapshot of one accounting group's progress, from `core/stats`.

    Always read from the operation's own explicit `_group`, never rclone's
    global stats - multiple embedded jobs may be in flight in the same
    process.
    """

    bytes: int
    total_bytes: int
    checks: int
    total_checks: int
    transfers: int
    total_transfers: int
    errors: int
    fatal_error: bool
    retry_error: bool
    speed: float
    eta_seconds: float | None
    elapsed_seconds: float
    active_transfers: tuple[ActiveTransfer, ...] = ()
    active_checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "bytes",
            "total_bytes",
            "checks",
            "total_checks",
            "transfers",
            "total_transfers",
            "errors",
        ):
            _require_non_negative_int(name, getattr(self, name))
        _require_non_negative("speed", self.speed)
        _require_non_negative("elapsed_seconds", self.elapsed_seconds)
        if self.eta_seconds is not None:
            _require_non_negative("eta_seconds", self.eta_seconds)


@dataclass(frozen=True)
class OperationAttempt:
    """One command-level retry attempt of a downstream operation.

    Not a subprocess: has no PID, args, stdout, stderr, or return code.
    """

    number: int
    started_at: datetime
    ended_at: datetime
    duration: float
    ok: bool
    error: str | None
    fatal_error: bool
    retry_error: bool

    def __post_init__(self) -> None:
        _require_non_negative_int("number", self.number)
        if self.number < 1:
            raise ValueError(f"number must be >= 1, got {self.number!r}")
        _require_aware("started_at", self.started_at)
        _require_aware("ended_at", self.ended_at)
        _require_non_negative("duration", self.duration)
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")


@dataclass(frozen=True)
class OperationWarning:
    """A non-fatal condition surfaced alongside an otherwise-successful
    (or independently-failed) operation."""

    message: str
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _freeze_mapping(self.detail))


@dataclass(frozen=True)
class OperationResult:
    """The execution-independent result of one completed (non-streaming)
    embedded operation.

    `job_ids` is a tuple even though most operations only ever produce one:
    partitioned operations (files-from copies/deletes) aggregate several
    jobs into a single `OperationResult` without needing a new result type.

    `ok` is stored explicitly and validated against `cancelled`/`error`
    rather than inferred from an empty error string alone.
    """

    ok: bool
    operation: str
    source: str | None
    destination: str | None
    job_ids: tuple[int, ...]
    stats: TransferStats | None
    warnings: tuple[OperationWarning, ...]
    attempts: tuple[OperationAttempt, ...]
    started_at: datetime
    ended_at: datetime
    duration: float
    cancelled: bool
    error: str | None

    def __post_init__(self) -> None:
        _require_aware("started_at", self.started_at)
        _require_aware("ended_at", self.ended_at)
        _require_non_negative("duration", self.duration)
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")
        if self.ok and self.error is not None:
            raise ValueError("ok=True cannot carry a non-None error")
        if not self.ok and self.error is None and not self.cancelled:
            raise ValueError("ok=False requires either error or cancelled to explain why")
        if self.cancelled and self.ok:
            raise ValueError("a cancelled operation cannot also be ok=True")
