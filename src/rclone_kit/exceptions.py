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
