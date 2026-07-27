"""RC-level (rclone HTTP-like status) error types.

Distinct from `rclone_kit.native.errors`, which covers the ABI's own
reserved negative lifecycle codes (not-initialized, panic, and similar); an
`RcCallError` means the call dispatched but rclone itself reported failure.

Rooted in `RcloneKitError` so the documented `except RcloneKitError`
boundary handler catches a failed RC call - by far the most common way an
operation fails - rather than letting it escape as a bare `Exception`.
"""

from rclone_kit.exceptions import RcloneKitError


class RcCallError(RcloneKitError):
    """Raised when an RC method's status is not the expected success status.

    Carries the method name, the actual status, and the decoded JSON error
    body so callers can inspect rclone's own error message/type.
    """

    def __init__(self, method: str, status: int, payload: dict) -> None:
        self.method = method
        self.status = status
        self.payload = payload
        super().__init__(f"RC call {method!r} failed with status {status}: {payload}")
