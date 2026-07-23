"""RC job boundary: typed `start`/`status`/`stats`/`stop`/`delete_stats`
adapters over one `RcCallable`, translating rclone's `job/status` and
`core/stats` wire JSON into the domain values in `rclone_kit.operation`.

Wire shapes below were verified empirically against the pinned native
build (`sync/copy` started with `_async: true`, then polled), not merely
assumed from rclone's RC help text:

- a start call (any method plus `_async: true`, `_group: <group>`) returns
  exactly `{"executeId": <str>, "jobid": <int>}`;
- `job/status` returns `duration` (float seconds), `endTime`/`startTime`
  (RFC3339, or Go's zero time `"0001-01-01T00:00:00Z"` before the job
  finishes), `error` (empty string means none), `executeId`, `finished`
  (bool), `group`, `id`, `output` (a dict, or `null` before finishing), and
  `success` (bool);
- a `job/status`/`job/stop` call for an unknown job ID fails with RC status
  500 and `payload["error"] == "job not found"` - this is surfaced as
  `RcJobNotFoundError`, not left as a bare `RcCallError`, so a higher layer
  holding cached job state (the future job monitor) can decide whether this
  means the job's terminal record expired, a stale handle was reused, or
  something else - this module alone does not have enough context to know; and
  `job/stop` on an already-finished or unknown job is otherwise a no-op
  success, so `stop()` treats "not found" as already-idempotently-stopped
  rather than raising.
- `core/stats` returns per-group totals plus two *optional* arrays,
  present only when nonempty (never an empty list): `transferring`
  (`{"name", "bytes", "size", "speed", "percentage", "eta"}` per entry -
  see `native/rclone/fs/accounting/stats_groups.go`'s documented shape) and
  `checking` (a list of plain name strings).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from rclone_kit.exceptions import JobIdentityError, OperationStartError
from rclone_kit.operation import ActiveTransfer, JobState, JobStatus, TransferStats
from rclone_kit.rc.errors import RcCallError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rclone_kit.rc.client import RcCallable

_JOB_NOT_FOUND_MESSAGE = "job not found"
_GO_ZERO_TIME_YEAR = 1


class RcJobNotFoundError(Exception):
    """Raised when rclone reports no job exists for a given job ID.

    Deliberately not a domain `OperationError`: this is an RC-wire-level
    fact ("rclone has no record of this ID right now"), not yet a decision
    about what it *means* for the operation (expired, mismatched, lost).
    That decision needs cached job state this module does not have.
    """

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(f"rclone has no record of job {job_id}")


@dataclass(frozen=True)
class RcJobRef:
    """Identifies one rclone RC job: `job_id` alone is not a stable
    identity across rclone process restarts, so every consumer must carry
    and validate `execute_id` alongside it."""

    job_id: int
    execute_id: str
    group: str


class RcJobClient(Protocol):
    """Narrow job-control interface `JobHandle`/`_JobMonitor` depend on, so
    their tests can supply a fake without a real `RcClient`."""

    def start(self, method: str, params: Mapping[str, object], group: str) -> RcJobRef: ...
    def status(self, ref: RcJobRef) -> JobStatus: ...
    def stats(self, group: str) -> TransferStats: ...
    def stop(self, ref: RcJobRef) -> None: ...
    def delete_stats(self, group: str) -> None: ...


def _is_job_not_found(error: RcCallError) -> bool:
    return str(error.payload.get("error", "")) == _JOB_NOT_FOUND_MESSAGE


def _parse_go_time(value: object, *, field: str) -> datetime | None:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {value!r}")
    parsed = datetime.fromisoformat(value)
    if parsed.year == _GO_ZERO_TIME_YEAR:
        return None
    if parsed.tzinfo is None:
        raise ValueError(f"{field} {value!r} parsed without a timezone")
    return parsed


def _require_str(payload: Mapping[str, object], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {value!r}")
    return value


def _require_int(payload: Mapping[str, object], field: str) -> int:
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an int, got {value!r}")
    return value


def _require_number(payload: Mapping[str, object], field: str) -> float:
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} must be a number, got {value!r}")
    return float(value)


def _require_bool(payload: Mapping[str, object], field: str) -> bool:
    value = payload[field]
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a bool, got {value!r}")
    return value


def _optional_number(payload: Mapping[str, object], field: str) -> float | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} must be a number or null, got {value!r}")
    return float(value)


def _optional_percentage(payload: Mapping[str, object], field: str) -> int | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an int or null, got {value!r}")
    return value


def _parse_job_status(ref: RcJobRef, payload: Mapping[str, object]) -> JobStatus:
    finished = _require_bool(payload, "finished")
    success = _require_bool(payload, "success")
    error = _require_str(payload, "error") or None
    started_at = _parse_go_time(payload["startTime"], field="startTime")
    ended_at = _parse_go_time(payload["endTime"], field="endTime") if finished else None
    if started_at is None:
        raise ValueError(f"startTime {payload['startTime']!r} must not be the zero time")

    if not finished:
        state = JobState.RUNNING
    elif success:
        state = JobState.SUCCEEDED
    else:
        state = JobState.FAILED

    output = payload.get("output") or {}
    if not isinstance(output, dict):
        raise ValueError(f"output must be a dict or null, got {output!r}")

    return JobStatus(
        job_id=ref.job_id,
        execute_id=ref.execute_id,
        group=ref.group,
        state=state,
        started_at=started_at,
        ended_at=ended_at,
        duration=_require_number(payload, "duration"),
        error=error,
        output=output,
    )


def _parse_active_transfer(entry: object) -> ActiveTransfer:
    if not isinstance(entry, dict):
        raise ValueError(f"transferring entry must be an object, got {entry!r}")
    return ActiveTransfer(
        name=_require_str(entry, "name"),
        bytes=_require_int(entry, "bytes"),
        size=_require_int(entry, "size"),
        speed=_require_number(entry, "speed"),
        percentage=_optional_percentage(entry, "percentage"),
        eta_seconds=_optional_number(entry, "eta"),
    )


def _parse_transfer_stats(payload: Mapping[str, object]) -> TransferStats:
    transferring = payload.get("transferring") or []
    if not isinstance(transferring, list):
        raise ValueError(f"transferring must be a list, got {transferring!r}")
    checking = payload.get("checking") or []
    if not isinstance(checking, list):
        raise ValueError(f"checking must be a list, got {checking!r}")
    for name in checking:
        if not isinstance(name, str):
            raise ValueError(f"checking entries must be strings, got {name!r}")

    return TransferStats(
        bytes=_require_int(payload, "bytes"),
        total_bytes=_require_int(payload, "totalBytes"),
        checks=_require_int(payload, "checks"),
        total_checks=_require_int(payload, "totalChecks"),
        transfers=_require_int(payload, "transfers"),
        total_transfers=_require_int(payload, "totalTransfers"),
        errors=_require_int(payload, "errors"),
        fatal_error=_require_bool(payload, "fatalError"),
        retry_error=_require_bool(payload, "retryError"),
        speed=_require_number(payload, "speed"),
        eta_seconds=_optional_number(payload, "eta"),
        elapsed_seconds=_require_number(payload, "elapsedTime"),
        active_transfers=tuple(_parse_active_transfer(entry) for entry in transferring),
        active_checks=tuple(checking),
    )


class RcloneRcJobClient:
    """The real `RcJobClient`, backed by one `RcCallable` (usually an
    `RcClient`/`RcloneRuntime` pair, but any object satisfying the narrow
    protocol works - tests supply a fake)."""

    def __init__(self, rc_client: RcCallable) -> None:
        self._rc_client = rc_client

    def start(self, method: str, params: Mapping[str, object], group: str) -> RcJobRef:
        try:
            response = self._rc_client.call(method, _async=True, _group=group, **dict(params))
        except RcCallError as error:
            raise OperationStartError(method, error) from error
        return RcJobRef(
            job_id=_require_int(response, "jobid"),
            execute_id=_require_str(response, "executeId"),
            group=group,
        )

    def status(self, ref: RcJobRef) -> JobStatus:
        try:
            response = self._rc_client.call("job/status", jobid=ref.job_id)
        except RcCallError as error:
            if _is_job_not_found(error):
                raise RcJobNotFoundError(ref.job_id) from error
            raise
        actual_execute_id = _require_str(response, "executeId")
        if actual_execute_id != ref.execute_id:
            raise JobIdentityError(ref.job_id, ref.execute_id, actual_execute_id)
        return _parse_job_status(ref, response)

    def stats(self, group: str) -> TransferStats:
        response = self._rc_client.call("core/stats", group=group)
        return _parse_transfer_stats(response)

    def stop(self, ref: RcJobRef) -> None:
        try:
            self._rc_client.call("job/stop", jobid=ref.job_id)
        except RcCallError as error:
            if _is_job_not_found(error):
                return
            raise

    def delete_stats(self, group: str) -> None:
        self._rc_client.call("core/stats-delete", group=group)
