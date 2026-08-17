"""RC job boundary: typed `start`/`status`/`statuses`/`stats`/`stop`/
`delete_stats` adapters over one `RcCallable`, translating rclone's
`job/status` and `core/stats` wire JSON into the domain values in
`rclone_kit.operation`.

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
  holding cached job state (`_JobMonitor` in `job.py`) can decide whether
  this means the job's terminal record expired, a stale handle was reused, or
  something else - this module alone does not have enough context to know; and
  `job/stop` on an already-finished or unknown job is otherwise a no-op
  success, so `stop()` treats "not found" as already-idempotently-stopped
  rather than raising.
- `core/stats` returns per-group totals plus two *optional* arrays,
  present only when nonempty (never an empty list): `transferring`
  (`{"name", "bytes", "size", "speed", "percentage", "eta"}` per entry -
  see `native/rclone/fs/accounting/stats_groups.go`'s documented shape) and
  `checking` (a list of plain name strings).
- `rclonekit/copy`'s `job/status.output` is `{"attempts": [...]}`, one
  entry per high-level retry attempt (`copyAttempt` in
  `librclone/rclonekit/rc/copy.go`): `{"number", "startTime", "endTime",
  "duration", "ok", "error" (omitted when empty), "fatalError",
  "retryError"}`. Every other RC method's `output` simply lacks this key.

`job/batch`'s shape is read from the pinned source (`rcBatch` in
`native/rclone/fs/rc/jobs/job.go`) rather than probed, since it is only
reachable through a built native library:

- it takes `inputs`, a list of RC parameter objects each carrying an extra
  `_path` naming the method to run, and returns `results` - one entry per
  input, at the same index, even for inputs that failed;
- a failing entry does *not* fail the whole call. It comes back as an
  ordinary `rc.Error` blob in its own slot (`{"error", "status", "input",
  "path"}`, see `Error` in `fs/rc/params.go`), so per-entry failures are
  data here and must be mapped back to the very exceptions the single-job
  `status()` path raises;
- an entry is therefore only a real `job/status` payload if it carries
  `finished`. Keying off `error` alone would misread a *successfully*
  polled but failed job - whose payload legitimately carries a non-empty
  `error` - as a batch-level failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from rclone_kit.exceptions import JobIdentityError, OperationStartError, RcloneKitError
from rclone_kit.operation import (
    ActiveTransfer,
    JobState,
    JobStatus,
    OperationAttempt,
    TransferStats,
)
from rclone_kit.rc.errors import RcCallError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from rclone_kit.rc.client import RcCallable

_JOB_NOT_FOUND_MESSAGE = "job not found"
_GO_ZERO_TIME_YEAR = 1
_STATUS_METHOD = "job/status"
_BATCH_METHOD = "job/batch"
_BATCH_INPUTS_FIELD = "inputs"
_BATCH_RESULTS_FIELD = "results"
_BATCH_PATH_FIELD = "_path"
_JOB_STATUS_MARKER_FIELD = "finished"
_RC_ERROR_STATUS_FIELD = "status"
_UNREPORTED_ERROR_STATUS = 500

type RcJobStatusResult = JobStatus | Exception
"""One job's poll outcome as a value rather than control flow: either its
parsed `JobStatus`, or the exception a single-job `status()` call would
have raised for it. Batched polling has to carry per-job failures
alongside per-job successes, which an exception cannot express."""


class RcJobNotFoundError(RcloneKitError):
    """Raised when rclone reports no job exists for a given job ID.

    Deliberately not a domain `OperationError`: this is an RC-wire-level
    fact ("rclone has no record of this ID right now"), not yet a decision
    about what it *means* for the operation (expired, mismatched, lost).
    That decision needs cached job state this module does not have.

    Still rooted in `RcloneKitError` - staying outside the `OperationError`
    branch is about *which* kind of failure this is, not about escaping the
    library-wide root every caller's boundary handler catches.
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


@runtime_checkable
class RcBatchStatusClient(Protocol):
    """The *optional* batched-polling capability on top of `RcJobClient`.

    Deliberately a second protocol rather than a widening of `RcJobClient`:
    polling many jobs in one round-trip is an optimization, and every
    consumer must still work against a client that only offers `status()`.
    `_JobMonitor` probes for it with `isinstance()` and falls back to
    per-job polling when it is absent, so a fake (or a future alternative
    client) never has to implement it just to stay usable.
    """

    def statuses(self, refs: Sequence[RcJobRef]) -> list[RcJobStatusResult]: ...


def _is_job_not_found(payload: Mapping[str, object]) -> bool:
    """Report rclone's "job not found" fact, wherever it surfaced.

    The same condition arrives two ways: raised as an `RcCallError` from a
    direct `job/status`/`job/stop` call, and as plain data inside one
    `job/batch` result entry. Both must reach `RcJobNotFoundError`.
    """
    return str(payload.get("error", "")) == _JOB_NOT_FOUND_MESSAGE


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


def _parse_operation_attempt(entry: object) -> OperationAttempt:
    if not isinstance(entry, dict):
        raise ValueError(f"attempt entry must be an object, got {entry!r}")
    started_at = _parse_go_time(entry["startTime"], field="attempt startTime")
    ended_at = _parse_go_time(entry["endTime"], field="attempt endTime")
    if started_at is None:
        raise ValueError(f"attempt startTime {entry['startTime']!r} must not be the zero time")
    if ended_at is None:
        raise ValueError(f"attempt endTime {entry['endTime']!r} must not be the zero time")
    return OperationAttempt(
        number=_require_int(entry, "number"),
        started_at=started_at,
        ended_at=ended_at,
        duration=_require_number(entry, "duration"),
        ok=_require_bool(entry, "ok"),
        error=entry.get("error") or None,
        fatal_error=_require_bool(entry, "fatalError"),
        retry_error=_require_bool(entry, "retryError"),
    )


def parse_operation_attempts(output: Mapping[str, object]) -> tuple[OperationAttempt, ...]:
    """Parse `rclonekit/copy`'s `output["attempts"]` array (`copyAttempt` in
    `librclone/rclonekit/rc/copy.go`) into `OperationAttempt`s.

    Any other RC method's `output` has no `"attempts"` key, so this returns
    `()` for it - the same value `OperationResult.attempts` always had
    before this parser existed.
    """
    attempts = output.get("attempts")
    if attempts is None:
        return ()
    if not isinstance(attempts, list):
        raise ValueError(f"attempts must be a list, got {attempts!r}")
    return tuple(_parse_operation_attempt(entry) for entry in attempts)


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


def _validated_job_status(ref: RcJobRef, payload: Mapping[str, object]) -> JobStatus:
    """Parse `payload` as `ref`'s status, rejecting with `JobIdentityError`
    a payload that belongs to a different rclone execution."""
    actual_execute_id = _require_str(payload, "executeId")
    if actual_execute_id != ref.execute_id:
        raise JobIdentityError(ref.job_id, ref.execute_id, actual_execute_id)
    return _parse_job_status(ref, payload)


def _batch_entry_failure(ref: RcJobRef, entry: Mapping[str, object]) -> Exception:
    """Turn one `job/batch` `rc.Error` blob back into the exception the
    single-job `status()` path would have raised for that job alone."""
    if _is_job_not_found(entry):
        return RcJobNotFoundError(ref.job_id)
    status = entry.get(_RC_ERROR_STATUS_FIELD)
    reported_status = status if isinstance(status, int) else _UNREPORTED_ERROR_STATUS
    return RcCallError(_STATUS_METHOD, reported_status, dict(entry))


def _batch_status_result(ref: RcJobRef, entry: object) -> RcJobStatusResult:
    """Interpret one `job/batch` result entry as `ref`'s poll outcome.

    Every failure mode here is scoped to this one job: a malformed or
    failed entry becomes that job's own exception, never the batch's, so
    its siblings in the same response are unaffected.
    """
    if not isinstance(entry, dict):
        return ValueError(f"{_BATCH_METHOD} result must be an object, got {entry!r}")
    if _JOB_STATUS_MARKER_FIELD not in entry:
        return _batch_entry_failure(ref, entry)
    try:
        return _validated_job_status(ref, entry)
    except Exception as error:
        return error


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
            response = self._rc_client.call(_STATUS_METHOD, jobid=ref.job_id)
        except RcCallError as error:
            if _is_job_not_found(error.payload):
                raise RcJobNotFoundError(ref.job_id) from error
            raise
        return _validated_job_status(ref, response)

    def statuses(self, refs: Sequence[RcJobRef]) -> list[RcJobStatusResult]:
        """Poll every ref in `refs` in a single `job/batch` round-trip,
        returning one outcome per ref in the order given.

        Raises only on a *whole-call* failure - an unusable response shape,
        or `job/batch` itself failing (a native build predating it answers
        404). A caller must treat that as "this transport is unavailable",
        not as a statement about any job, and re-poll through `status()`.
        Anything scoped to one job is reported as that entry's outcome
        instead, so one bad job never hides its siblings' progress.
        """
        if not refs:
            return []
        inputs = [{_BATCH_PATH_FIELD: _STATUS_METHOD, "jobid": ref.job_id} for ref in refs]
        response = self._rc_client.call(_BATCH_METHOD, **{_BATCH_INPUTS_FIELD: inputs})
        results = response[_BATCH_RESULTS_FIELD]
        if not isinstance(results, list):
            raise ValueError(f"{_BATCH_RESULTS_FIELD} must be a list, got {results!r}")
        if len(results) != len(refs):
            raise ValueError(
                f"{_BATCH_METHOD} returned {len(results)} results for {len(refs)} inputs"
            )
        return [_batch_status_result(ref, entry) for ref, entry in zip(refs, results, strict=True)]

    def stats(self, group: str) -> TransferStats:
        response = self._rc_client.call("core/stats", group=group)
        return _parse_transfer_stats(response)

    def stop(self, ref: RcJobRef) -> None:
        try:
            self._rc_client.call("job/stop", jobid=ref.job_id)
        except RcCallError as error:
            if _is_job_not_found(error.payload):
                return
            raise

    def delete_stats(self, group: str) -> None:
        self._rc_client.call("core/stats-delete", group=group)
