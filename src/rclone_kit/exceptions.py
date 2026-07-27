"""Exception types raised by rclone-kit's non-runtime operations.

`RcloneKitError` is the root of the *whole* library's hierarchy, not just
this module's: the per-subsystem base types each subclass it -
`rclone_kit.rc.errors.RcCallError`, `rclone_kit.native.errors.NativeError`,
`rclone_kit.rc.jobs.RcJobNotFoundError` and
`rclone_kit.runtime.exceptions.RcloneRuntimeError`. Each keeps its own
module so its subsystem stays self-describing, but a caller writing the
boundary handler `docs/production_usage.md` recommends - `except
RcloneKitError` - catches every one of them.

That root is the only guarantee callers should rely on for "something in
rclone-kit failed". `MissingOptionalDependencyError` deliberately stays
outside it: it subclasses `ImportError` because it reports a deployment
packaging fault, not a storage operation failing, and callers are meant to
treat it as permanent rather than folding it into a retry policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rclone_kit.operation import OperationResult


class RcloneKitError(Exception):
    """Base type for every error raised by rclone-kit's own operations."""


class FilesystemError(RcloneKitError):
    """Raised when a local or remote filesystem operation fails for a
    reason other than the target simply not existing.

    `fs.filesystem` raises `FileNotFoundError` directly for missing-target
    cases, consistent with its other not-found paths.
    """

    def __init__(self, path: str, cause: Exception) -> None:
        self.path = path
        self.cause = cause
        super().__init__(f"Filesystem operation failed for {path!r}: {cause}")


class ConfigParseError(RcloneKitError):
    """Raised when a JSON dict cannot be converted to rclone config text.

    Carries the original failure (malformed JSON, or a value shape that
    isn't a mapping of section name to key/value pairs) as `__cause__`.
    """

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(f"Failed to convert JSON to rclone config: {cause}")


class RcloneCommandError(RcloneKitError):
    """Raised when one of the operations named after an rclone command
    (`copyto`, `cat`, `liststream`) fails.

    No subprocess is involved: these run against the embedded runtime, so
    `stderr` carries whatever diagnostic text the failure produced -
    `OperationResult.error` for a failed job, or the stream's own error
    for a listing - and `cause` is the underlying `OperationError`,
    `FileNotFoundError`, or `RuntimeError`, also set as `__cause__`. The
    attribute keeps its `stderr` name because it is part of this type's
    published surface, not because a process wrote to a pipe.
    """

    def __init__(self, command: str, stderr: str, cause: Exception) -> None:
        self.command = command
        self.stderr = stderr
        self.cause = cause
        super().__init__(f"rclone {command} failed: {stderr or cause}")


class HttpFetchError(RcloneKitError):
    """Raised when a request to rclone's `serve http` fails: a non-2xx
    response, a transport-level error, or an incomplete ranged download.

    Carries the remote path and the underlying failure as `__cause__`.
    """

    def __init__(self, path: str, cause: Exception) -> None:
        self.path = path
        self.cause = cause
        super().__init__(f"HTTP fetch failed for {path!r}: {cause}")


class MergeStateError(RcloneKitError):
    """Raised when S3 multipart merge-state JSON is malformed: a part
    entry missing `part_number`/`s3_key`, or a required top-level key.

    Carries the offending JSON fragment as `detail`.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Invalid merge-state JSON: {detail}")


class S3MergeError(RcloneKitError):
    """Raised when an S3 server-side multipart merge fails: a part copy
    exhausts its retries, completing the upload fails, or the merged
    object's state is invalid (no parts loaded, a finished-parts count
    mismatch, or a failed cleanup purge).
    """


class OperationError(RcloneKitError):
    """Base type for the execution-independent embedded operation errors
    (`JobHandle`/`start_*` family).

    Raised by operations when the operation itself starts, fails, is
    cancelled, times out, or its job identity/lifecycle cannot be trusted.
    """


class OperationStartError(OperationError):
    """Raised when the RC call that would create a job fails before any
    job ID exists.

    Carries the underlying `RcCallError` (or similar) as `__cause__`.
    """

    def __init__(self, operation: str, cause: Exception) -> None:
        self.operation = operation
        self.cause = cause
        super().__init__(f"Failed to start operation {operation!r}: {cause}")


class OperationFailedError(OperationError):
    """Raised when `check=True` and a started operation reaches a failed
    terminal state.

    Carries the complete `OperationResult` as `result`, so callers can
    inspect attempts/stats/warnings even though the operation raised.
    """

    def __init__(self, result: OperationResult) -> None:
        self.result = result
        super().__init__(f"Operation {result.operation!r} failed: {result.error}")


class OperationCancelledError(OperationError):
    """Raised when `check=True` and a started operation reaches a
    confirmed-cancelled terminal state.

    Carries the complete `OperationResult` as `result`.
    """

    def __init__(self, result: OperationResult) -> None:
        self.result = result
        super().__init__(f"Operation {result.operation!r} was cancelled")


class OperationTimeoutError(OperationError):
    """Raised when `JobHandle.wait(timeout=...)` elapses before the job
    reaches a terminal state.

    The timeout bounds observation only; it never cancels the already-
    dispatched operation. Callers that want cancellation-on-timeout must
    call `cancel()` themselves.
    """

    def __init__(self, operation: str, timeout: float) -> None:
        self.operation = operation
        self.timeout = timeout
        super().__init__(
            f"Timed out after {timeout}s waiting for operation {operation!r} to finish"
        )


class JobExpiredError(OperationError):
    """Raised when a job's terminal state was lost to rclone's own
    `job/status` expiry window before this client observed it.

    Distinct from an ordinary operation failure: the outcome is genuinely
    unknown, not failed.
    """

    def __init__(self, job_id: int, execute_id: str) -> None:
        self.job_id = job_id
        self.execute_id = execute_id
        super().__init__(
            f"Job {job_id} (execute_id={execute_id!r}) expired before its terminal "
            "state could be observed"
        )


class JobIdentityError(OperationError):
    """Raised when a job's `execute_id` does not match the value recorded
    when the job was started.

    Rclone job IDs restart from one whenever the rclone process restarts,
    so `job_id` alone is not a stable identity; a mismatch here means a
    handle would otherwise silently attach to an unrelated operation.
    """

    def __init__(self, job_id: int, expected_execute_id: str, actual_execute_id: str) -> None:
        self.job_id = job_id
        self.expected_execute_id = expected_execute_id
        self.actual_execute_id = actual_execute_id
        super().__init__(
            f"Job {job_id} identity mismatch: expected execute_id="
            f"{expected_execute_id!r}, got {actual_execute_id!r}"
        )


class JobRuntimeClosedError(OperationError):
    """Raised when the runtime a job was being polled through was closed
    before that job reached a terminal state.

    Like `JobExpiredError`, the outcome is genuinely unknown rather than
    failed: the operation may well have completed inside rclone, but the
    only channel that could have reported it is gone. `RcloneRuntime`'s
    closed flag is a one-way latch, so this can never resolve by retrying.
    """

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(
            f"The runtime polling job {job_id} was closed before the job reached a "
            "terminal state; its outcome can no longer be observed"
        )


class OperationShutdownError(OperationError):
    """Raised when an owned `Rclone` client cannot safely cancel/observe
    every job it started within its shutdown deadline.

    The runtime is left open (not finalized) when this is raised, so the
    caller may retry cancellation or wait longer rather than losing access
    to a client whose jobs are still live.
    """


class S3UploadError(RcloneKitError):
    """Raised when a resumable S3 multipart upload fails to upload one or
    more chunks after retries are exhausted.

    Carries the individual per-part failures as `errors`.
    """

    def __init__(self, errors: list[Exception]) -> None:
        self.errors = errors
        super().__init__(f"Failed to upload {len(errors)} part(s): {errors}")
