"""Exception types raised across the rclone runtime package.

Centralizing every runtime exception here keeps `platform.py`,
`archive_extract.py`, `hashing.py`, `downloader.py`, and `rclone_binary.py`
free of duplicated error definitions and avoids import cycles between them.
"""

from pathlib import Path


class RcloneRuntimeError(Exception):
    """Base type for every exception raised by `rclone_kit.runtime`."""


class UnsupportedPlatformError(RcloneRuntimeError):
    """Raised when the running operating system or machine architecture has
    no certified rclone build target.

    Carries the raw, unnormalized `system` and `machine` values so callers
    can produce a precise diagnostic without re-deriving them.
    """

    def __init__(self, system: str, machine: str) -> None:
        self.system = system
        self.machine = machine
        super().__init__(f"Unsupported platform: system={system!r} machine={machine!r}")


class ArchiveMemberMissingError(RcloneRuntimeError):
    """Raised when an expected member name is absent from a zip archive."""

    def __init__(self, member_name: str) -> None:
        self.member_name = member_name
        super().__init__(f"Archive member not found: {member_name!r}")


class ArchiveMemberDuplicateError(RcloneRuntimeError):
    """Raised when a zip archive contains more than one entry for the same
    expected member name.
    """

    def __init__(self, member_name: str) -> None:
        self.member_name = member_name
        super().__init__(f"Archive member appears more than once: {member_name!r}")


class ArchiveMemberUnsafeError(RcloneRuntimeError):
    """Raised when a zip member's recorded path is absolute or escapes the
    archive root through a parent-directory (`..`) segment.
    """

    def __init__(self, member_name: str) -> None:
        self.member_name = member_name
        super().__init__(f"Unsafe archive member path: {member_name!r}")


class ArchiveDownloadHttpError(RcloneRuntimeError):
    """Raised when downloading an rclone release archive fails at the HTTP
    or transport layer.
    """

    def __init__(self, url: str, status_code: int | None) -> None:
        self.url = url
        self.status_code = status_code
        detail = f"status={status_code}" if status_code is not None else "transport error"
        super().__init__(f"Failed to download archive from {url!r}: {detail}")


class ArchiveTruncatedDownloadError(RcloneRuntimeError):
    """Raised when fewer bytes arrive than the server advertised via
    `Content-Length`.
    """

    def __init__(self, url: str, expected_bytes: int, actual_bytes: int) -> None:
        self.url = url
        self.expected_bytes = expected_bytes
        self.actual_bytes = actual_bytes
        super().__init__(
            f"Truncated download from {url!r}: expected {expected_bytes} bytes, got {actual_bytes}"
        )


class ArchiveDigestMismatchError(RcloneRuntimeError):
    """Raised when a downloaded archive's computed SHA-256 digest does not
    match the repository-controlled expected digest.
    """

    def __init__(self, url: str, expected_digest: str, actual_digest: str) -> None:
        self.url = url
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        super().__init__(
            f"SHA-256 mismatch for {url!r}: expected {expected_digest}, got {actual_digest}"
        )


class CacheReplacementError(RcloneRuntimeError):
    """Raised when atomically replacing a cache entry fails."""

    def __init__(self, destination: Path) -> None:
        self.destination = destination
        super().__init__(f"Failed to atomically replace cache entry at {destination}")


class StagedExecutableDigestMismatchError(RcloneRuntimeError):
    """Raised when a freshly extracted, build-time-staged rclone executable's
    SHA-256 digest disagrees with `RcloneArtifact.executable_sha256_digest`.

    Raised by `scripts/prepare_rclone_artifact.py` immediately after
    extraction, before the executable is written into any staging or wheel
    build directory, so a corrupted extraction can never be packaged.
    """

    def __init__(self, path: Path, expected_digest: str, actual_digest: str) -> None:
        self.path = path
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        super().__init__(
            f"Staged executable SHA-256 mismatch at {path}: "
            f"expected {expected_digest}, got {actual_digest}"
        )


class CacheVerificationError(RcloneRuntimeError):
    """Raised when a materialized cache copy does not match its expected
    SHA-256 digest.
    """

    def __init__(self, destination: Path, expected_digest: str, actual_digest: str) -> None:
        self.destination = destination
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        super().__init__(
            f"SHA-256 mismatch for cache entry {destination}: "
            f"expected {expected_digest}, got {actual_digest}"
        )


class ExplicitExecutableNotFoundError(RcloneRuntimeError):
    """Raised when a caller-supplied explicit rclone executable path does not
    exist or is not a regular file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"Explicit rclone executable path is not a file: {path}")


class RcloneResolutionError(RcloneRuntimeError):
    """Raised when every enabled resolution strategy fails to locate a
    usable rclone executable.
    """

    def __init__(self, attempted_strategies: list[str]) -> None:
        self.attempted_strategies = attempted_strategies
        joined = ", ".join(attempted_strategies)
        super().__init__(f"Could not resolve an rclone executable. Tried: {joined}")
