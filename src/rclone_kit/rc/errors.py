"""RC-level (rclone HTTP-like status) error types.

Distinct from `rclone_kit.native.errors`, which covers the ABI's own
reserved negative lifecycle codes (not-initialized, panic, and similar); an
`RcCallError` means the call dispatched but rclone itself reported failure.
"""


class RcCallError(Exception):
    """Raised when an RC method's status is not the expected success status.

    Carries the method name, the actual status, and the decoded JSON error
    body so callers can inspect rclone's own error message/type.
    """

    def __init__(self, method: str, status: int, payload: dict) -> None:
        self.method = method
        self.status = status
        self.payload = payload
        super().__init__(f"RC call {method!r} failed with status {status}: {payload}")
