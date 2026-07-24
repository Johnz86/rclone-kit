"""Exception types for the native C ABI runtime layer.

Mirrors `rclone_kit.runtime.exceptions`'s pattern of a single base type per
subsystem, kept separate because these describe the rclone-kit-owned
`librclone_kit` ABI lifecycle, not the downloaded-executable runtime.
"""

from pathlib import Path


class NativeError(Exception):
    """Base type for every exception raised by `rclone_kit.native`."""


class LibraryNotFoundError(NativeError):
    """Raised when no usable native library path could be resolved.

    `path` is `None` when no candidate was even attempted (no explicit path,
    no environment override, and no packaged wheel asset present in the
    installed package); otherwise it is the specific candidate path that did
    not exist.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        if path is None:
            super().__init__(
                "No native library path was given, RCLONE_KIT_LIBRARY is unset, and no "
                "packaged wheel asset was found for this platform."
            )
        else:
            super().__init__(f"Native library not found: {path}")


class LibraryVerificationError(NativeError):
    """Raised when a packaged wheel asset's SHA-256 digest disagrees with its
    sibling `native-manifest.json`'s recorded digest.

    A mismatch here means the installed package is corrupted or was tampered
    with after being built; the library is never loaded in that case.
    """

    def __init__(self, path: Path, expected_digest: str, actual_digest: str) -> None:
        self.path = path
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        super().__init__(
            f"SHA-256 mismatch for packaged native library {path}: "
            f"expected {expected_digest}, got {actual_digest}"
        )


class AbiVersionMismatchError(NativeError):
    """Raised when a loaded library's `RcloneKitABIVersion()` does not match
    the ABI version this Python binding was written against.
    """

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"Native library ABI version {actual} does not match expected {expected}.")


class NativeInvalidInputError(NativeError):
    """Raised for `RCLONEKIT_ERR_INVALID_INPUT` (-1): null input with nonzero
    length, or malformed UTF-8/JSON.
    """


class NativeNotInitializedError(NativeError):
    """Raised for `RCLONEKIT_ERR_NOT_INITIALIZED` (-2): an RPC/finalize call
    before `initialize()`, or a call on this Python wrapper before it called
    `initialize()` itself.
    """


class NativeAlreadyInitializedError(NativeError):
    """Raised for `RCLONEKIT_ERR_ALREADY_INITIALIZED` (-3): more than one
    `initialize()` call in this process, or on this `RcloneRuntime`.
    """


class NativePanicError(NativeError):
    """Raised for `RCLONEKIT_ERR_PANIC` (-4): a Go panic was recovered while
    handling the call.
    """


class RuntimeClosedError(NativeError):
    """Raised when a method is called on an `RcloneRuntime` after `close()`."""

    def __init__(self) -> None:
        super().__init__("RcloneRuntime is closed and cannot be used.")
