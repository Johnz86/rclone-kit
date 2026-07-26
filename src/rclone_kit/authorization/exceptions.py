"""Exception types raised by `rclone_kit.authorization`.

Layered on `rclone_kit.exceptions.RcloneKitError`, following its established
shape: typed fields, a clear `__cause__` where one exists, no bare strings
where a value belongs on the exception.
"""

from __future__ import annotations

from rclone_kit.authorization.redaction import redact_provider_text
from rclone_kit.exceptions import RcloneKitError


class AuthorizationError(RcloneKitError):
    """Base type for every error raised by an authorization session."""


class AuthorizationRemoteNameConflictError(AuthorizationError):
    """Raised when `remote_name` already exists in the shared config and
    `on_conflict` is `RemoteConflictPolicy.REJECT` (the default).

    Raised before any RC call is made - `CreateRemote` would otherwise
    silently delete the existing section with this name.
    """

    def __init__(self, remote_name: str) -> None:
        self.remote_name = remote_name
        super().__init__(
            f"Remote {remote_name!r} already exists; pass "
            "on_conflict=RemoteConflictPolicy.RECONNECT to replace its token"
        )


class AuthorizationUnsupportedPromptError(AuthorizationError):
    """Raised when the non-interactive config state machine reaches a
    `config_*` question outside the driver's fixed, known answer policy.

    The driver never guesses an answer to a question it does not
    recognize.
    """

    def __init__(self, remote_name: str, state: str, option_name: str) -> None:
        self.remote_name = remote_name
        self.state = state
        self.option_name = option_name
        super().__init__(
            f"Remote {remote_name!r}: config state {state!r} asked an unsupported "
            f"question {option_name!r}"
        )


class AuthorizationStartError(AuthorizationError):
    """Raised when the initial `config/create`/`config/update` call fails
    before any question is reached.

    Carries the underlying `RcCallError` (or similar) as `__cause__` and
    `.cause` - that object is not redacted (removing `from cause` would
    fight this module's own "clear `__cause__`" convention), but this
    error's own message only ever embeds `.cause`'s text through
    `redact_provider_text`, since rclone's own error text can carry an
    attacker-influenced OAuth `error_description` straight through (see
    `docs/rclone_authorization_design.md`, "Secrets and logs").
    """

    def __init__(self, remote_name: str, cause: Exception) -> None:
        self.remote_name = remote_name
        self.cause = cause
        super().__init__(
            f"Failed to start authorization for {remote_name!r}: {redact_provider_text(str(cause))}"
        )


class AuthorizationRejectedError(AuthorizationError):
    """Raised when the provider or rclone reports the flow failed: a bad
    authorization code, a state mismatch, or an explicit provider denial.

    `reason` is stored already redacted via `redact_provider_text` - unlike
    `AuthorizationStartError.cause`, this is a plain string field with no
    `__cause__`/traceback of its own to preserve, so there is no reason to
    keep the raw provider text anywhere on this exception.
    """

    def __init__(self, remote_name: str, reason: str) -> None:
        self.remote_name = remote_name
        self.reason = redact_provider_text(reason)
        super().__init__(f"Authorization for {remote_name!r} was rejected: {self.reason}")


class AuthorizationExpiredError(AuthorizationError):
    """Raised when `expires_at` elapses before the session reaches a
    terminal state, whether it was still queued or already active."""

    def __init__(self, remote_name: str) -> None:
        self.remote_name = remote_name
        super().__init__(f"Authorization for {remote_name!r} expired before it completed")


class AuthorizationCancelledError(AuthorizationError):
    """Raised when `AuthorizationSession.cancel()` was called, or the
    manager force-cancelled an overrun session."""

    def __init__(self, remote_name: str) -> None:
        self.remote_name = remote_name
        super().__init__(f"Authorization for {remote_name!r} was cancelled")


class AuthorizationQueueFullError(AuthorizationError):
    """Raised when `AuthorizationManager.start()` is rejected because the
    pending queue is already at its configured cap (global or per-owner).

    Raised before the session is ever enqueued.
    """

    def __init__(self, remote_name: str, cap: int) -> None:
        self.remote_name = remote_name
        self.cap = cap
        super().__init__(
            f"Cannot start authorization for {remote_name!r}: pending-queue cap ({cap}) reached"
        )


class AuthorizationRelayError(AuthorizationError):
    """Raised when the relay could not reach or parse a response from
    rclone's private OAuth listener.

    Carries the underlying transport failure as `__cause__` when one
    exists. `detail` is redacted via `redact_provider_text` before being
    embedded in this error's message - a transport exception's own message
    can include the request URL, and the relay forwards the caller's raw
    callback query string verbatim (see `forward()`'s docstring), which is
    itself on the "Secrets and logs" sensitive-values list.
    """

    def __init__(self, detail: str, cause: Exception | None = None) -> None:
        self.cause = cause
        super().__init__(f"Authorization relay failed: {redact_provider_text(detail)}")
