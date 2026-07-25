"""Exception types raised across the rclone runtime package.

Centralizing every runtime exception here keeps `platform.py`,
`native_platform.py`, and `hashing.py` free of duplicated error definitions
and avoids import cycles between them.
"""


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


class ArchiveMemberUnsafeError(RcloneRuntimeError):
    """Raised when a zip member's recorded path is absolute or escapes the
    archive root through a parent-directory (`..`) segment.

    Used by `scripts/verify_distribution.py`'s own safe-extraction check
    when verifying a built wheel.
    """

    def __init__(self, member_name: str) -> None:
        self.member_name = member_name
        super().__init__(f"Unsafe archive member path: {member_name!r}")
