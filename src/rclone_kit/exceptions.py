"""Exception types raised by rclone-kit's non-runtime operations.

`rclone_kit.runtime` has its own `RcloneRuntimeError` hierarchy in
`runtime/exceptions.py` for platform/download/cache concerns; this module
covers everything else. Filled in incrementally as call sites that
currently return `Exception` as data are migrated to raise instead.
"""


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
    """Raised when an `rclone` subprocess invocation fails.

    Carries the rclone executable's stderr output, and the underlying
    `subprocess.CalledProcessError` or `OSError` as `__cause__`.
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
