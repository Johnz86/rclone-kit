"""Public value types for `rclone_kit.authorization`
(`docs/rclone_authorization_design.md`'s "Public API" and "Relay design"
sections)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rclone_kit.config import Config

_DEFAULT_EXPIRES_IN = timedelta(minutes=10)


class RemoteConflictPolicy(Enum):
    """What `AuthorizationManager.start()` does when `remote_name` already
    exists in the shared config."""

    REJECT = "reject"
    RECONNECT = "reconnect"


class AuthorizationStatus(Enum):
    """`AuthorizationSession` lifecycle. `QUEUED` is the one addition
    versus a per-process design: a session admitted immediately would
    never observe it, but one waiting for the single active slot does."""

    QUEUED = "queued"
    STARTING = "starting"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETING = "completing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    CLOSED = "closed"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {
        AuthorizationStatus.SUCCEEDED,
        AuthorizationStatus.FAILED,
        AuthorizationStatus.CANCELLED,
        AuthorizationStatus.EXPIRED,
        AuthorizationStatus.CLOSED,
    }
)


class Secret:
    """A small value type whose `repr`/`str` never expose its content.

    Deliberately not a dataclass: a dataclass's generated `__repr__`
    prints every field by value, exactly what this type exists to avoid.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    def __str__(self) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True)
class AuthorizationRequest:
    """One request to authorize a remote through rclone's own OAuth flow.

    `backend_options` contains ordinary rclone backend configuration (a
    Drive scope, and similar); it must not contain an already-issued
    token. `on_conflict` controls what happens when `remote_name` already
    exists in the shared config - see `RemoteConflictPolicy`.
    `private_listen_addr` almost never needs to be set: sessions are
    serialized, so there is no port-collision reason to vary it per
    session; `None` uses rclone's own default (loopback).

    `public_callback_url` is only needed for a *relay* deployment - a web
    service driving authorization on behalf of a browser elsewhere, which
    then goes through `AuthorizationManager.relay()`. Leave it `None` for
    the simpler, far more common case of a script or CLI tool running on
    the same machine as the browser that will complete consent: rclone's
    OAuth listener binds locally either way, and without a
    `public_callback_url` to redirect through, `AuthorizationSession.
    authorization_url` is simply that private listener's own URL, meant to
    be opened directly. This is also the *only* redirect URI rclone's own
    built-in shared client_id (used automatically when `client_id` is left
    `None`, same as plain interactive `rclone config create`) is
    registered for with the provider - overriding it would break that
    fallback, so a relay deployment normally supplies its own `client_id`/
    `client_secret` rather than mixing the two.
    """

    remote_name: str
    backend: str
    public_callback_url: str | None = None
    backend_options: Mapping[str, str] = field(default_factory=dict)
    client_id: str | None = None
    client_secret: Secret | None = None
    on_conflict: RemoteConflictPolicy = RemoteConflictPolicy.REJECT
    expires_in: timedelta = _DEFAULT_EXPIRES_IN
    private_listen_addr: str | None = None

    def __post_init__(self) -> None:
        if not self.remote_name:
            raise ValueError("remote_name must not be empty")
        if not self.backend:
            raise ValueError("backend must not be empty")
        if self.public_callback_url is not None and not self.public_callback_url:
            raise ValueError("public_callback_url must not be empty when given")
        if self.expires_in <= timedelta(0):
            raise ValueError(f"expires_in must be positive, got {self.expires_in!r}")


@dataclass(frozen=True)
class RelayRequest:
    """One inbound public-callback HTTP request for
    `AuthorizationManager.relay()` to translate and forward to rclone's
    private OAuth listener. Framework-neutral: a host application adapts
    its own request object into this shape."""

    path: str
    raw_query: bytes
    method: str = "GET"


@dataclass(frozen=True)
class RelayResponse:
    """`AuthorizationManager.relay()`'s translated response, ready for a
    host application to adapt back into its own response type."""

    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True)
class AuthorizationResult:
    """The outcome of a successful `AuthorizationSession`.

    `config` is scoped to exactly the one remote this session created or
    updated (via `rclonekit/configshow`), returned as a convenience for a
    caller that wants to store or transmit that credential independently -
    it is not the only place it now lives, since rclone already saved it
    into the runtime's shared config file as a durable side effect. Treat
    it as a secret.
    """

    remote_name: str
    config: Config
