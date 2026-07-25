"""`AuthorizationSession`: the public handle for one authorization
attempt, and the private `_SessionRecord`/driver+watcher threads that back
it (`docs/rclone_authorization_design.md`, "Public API proposal" and
"Session worker threads").

Mirrors `job.py`'s `JobHandle`/`_JobMonitor` shape: a mutable record owned
by the background worker(s), guarded by a `threading.Condition`; the
handle only ever reads the latest cached snapshot. Unlike a job, one
session needs *two* cooperating threads once admitted - a driver thread
that issues the sequential RC calls (parked for the whole browser wait
once it reaches the OAuth-completing call), and a short-lived status-
watcher thread that polls `config/oauthstatus` concurrently to capture
the OAuth URL while the driver thread is blocked. `session.py` is the
only module in `rclone_kit.authorization` that starts threads;
`AuthorizationManager` (a different module, to avoid a manager<->session
import cycle) owns the queue and calls `start_active()` once it has
admitted a record.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, Self
from urllib.parse import parse_qs, urlsplit, urlunsplit

from rclone_kit.authorization.exceptions import (
    AuthorizationCancelledError,
    AuthorizationError,
    AuthorizationExpiredError,
    AuthorizationRejectedError,
)
from rclone_kit.authorization.state_driver import build_call_functions, drive
from rclone_kit.authorization.types import AuthorizationStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from rclone_kit.authorization.types import AuthorizationRequest, AuthorizationResult
    from rclone_kit.rc.auth import RcAuthClient

logger = logging.getLogger(__name__)

_WATCHER_POLL_INTERVAL_SECONDS = 0.25
_WATCHER_JOIN_TIMEOUT_SECONDS = 5.0

# The exact error text `configSetup()` returns when its context is
# cancelled (`lib/oauthutil/oauthutil.go`), wrapped by `*oauth-do` into
# "config failed to refresh token: oauth authentication was cancelled".
# Distinguishes an intentional stop (cancel/expiry) from a real failure.
_OAUTH_CANCELLED_MARKER = "oauth authentication was cancelled"


@dataclasses.dataclass(eq=False)
class _SessionRecord:
    """Mutable state for one authorization session, owned by the driver/
    watcher threads and `AuthorizationManager`. All mutation and all reads
    of mutable fields happen under `condition`'s lock.

    `eq=False` keeps identity-based equality/hashing (the default
    `object.__eq__`/`__hash__`) rather than a dataclass-generated
    field-by-field comparison: `AuthorizationManager` looks records up in
    its pending `deque` by identity, and a mutable record with generated
    `__eq__` would also be unhashable.
    """

    id: str
    request: AuthorizationRequest
    owner: str | None
    expires_at: datetime
    condition: threading.Condition = dataclasses.field(default_factory=threading.Condition)
    status: AuthorizationStatus = AuthorizationStatus.QUEUED
    cancel_requested: bool = False
    expire_requested: bool = False
    authorization_url: str | None = None
    private_base_url: str | None = None
    oauth_state: str | None = None
    terminal_result: AuthorizationResult | None = None
    terminal_exception: Exception | None = None
    watcher_stop: threading.Event = dataclasses.field(default_factory=threading.Event)
    timer: threading.Timer | None = None
    generation: int = 0

    def _advance(self, status: AuthorizationStatus, **updates: object) -> None:
        """Move to a non-terminal `status`, or apply `updates` to an
        already-terminal record's status - a no-op either way once
        terminal, so a race against a concurrent settle can never revive
        or overwrite a session's true outcome."""
        with self.condition:
            if self.status.is_terminal:
                return
            self.status = status
            for key, value in updates.items():
                setattr(self, key, value)
            self.condition.notify_all()

    def mark_succeeded(self, result: AuthorizationResult) -> None:
        self._advance(AuthorizationStatus.SUCCEEDED, terminal_result=result)

    def mark_failed(self, exception: Exception) -> None:
        self._advance(AuthorizationStatus.FAILED, terminal_exception=exception)

    def mark_cancelled(self) -> None:
        self._advance(
            AuthorizationStatus.CANCELLED,
            terminal_exception=AuthorizationCancelledError(self.request.remote_name),
        )

    def mark_expired(self) -> None:
        self._advance(
            AuthorizationStatus.EXPIRED,
            terminal_exception=AuthorizationExpiredError(self.request.remote_name),
        )

    def mark_closed(self) -> None:
        """Only reached for a session that was still `QUEUED` - never
        started, so nothing at the rclone level was ever running to
        cancel; see `AuthorizationManager.close_session`."""
        self._advance(
            AuthorizationStatus.CLOSED,
            terminal_exception=AuthorizationCancelledError(self.request.remote_name),
        )


class _SessionManager(Protocol):
    """The subset of `AuthorizationManager`'s interface `AuthorizationSession`
    depends on, avoiding a manager<->session import cycle."""

    close_wait_seconds: float

    def cancel(self, record: _SessionRecord) -> None: ...
    def close_session(self, record: _SessionRecord) -> None: ...


class AuthorizationSession:
    """A handle to one in-flight or completed authorization attempt.

    Never exposes rclone's internal listener address or the raw
    `config/oauthstatus` payload - only the translated public URL and a
    typed lifecycle/result.
    """

    def __init__(self, manager: _SessionManager, record: _SessionRecord) -> None:
        self._manager = manager
        self._record = record
        # Set by `Rclone._track_authorization_session`, never by a caller -
        # removes this session from the client's tracking set once
        # disposed, mirroring `ServeHandle`/`MountHandle`'s own
        # `_on_dispose`.
        self._on_dispose: Callable[[], None] | None = None

    @property
    def id(self) -> str:
        return self._record.id

    @property
    def status(self) -> AuthorizationStatus:
        with self._record.condition:
            return self._record.status

    @property
    def expires_at(self) -> datetime:
        with self._record.condition:
            return self._record.expires_at

    @property
    def authorization_url(self) -> str:
        """Block until the session is admitted and rclone reports its
        URL, then return the public-facing URL. Bounded by `expires_at`,
        which may move forward once while this blocks (queued sessions
        are re-anchored to the full consent window at admission - see
        `AuthorizationRequest.expires_in`'s docstring).

        Raises the session's terminal exception if it fails, is
        cancelled, or expires before a URL is ever reported.
        """
        with self._record.condition:
            while self._record.authorization_url is None and not self._record.status.is_terminal:
                remaining = (self._record.expires_at - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    break
                self._record.condition.wait(timeout=remaining)
            if self._record.authorization_url is not None:
                return self._record.authorization_url
            terminal_exception = self._record.terminal_exception
        if terminal_exception is not None:
            raise terminal_exception
        raise AuthorizationExpiredError(self._record.request.remote_name)

    def wait(self, timeout: float | None = None) -> AuthorizationResult:
        """Block until the session reaches a terminal state.

        `timeout` bounds observation only. Raises `TimeoutError` if the
        deadline elapses first - call `cancel()` explicitly for
        cancel-on-timeout behavior.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._record.condition:
            while not self._record.status.is_terminal:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(
                        f"timed out after {timeout}s waiting for authorization "
                        f"{self._record.request.remote_name!r} to finish"
                    )
                self._record.condition.wait(timeout=remaining)
            terminal_exception = self._record.terminal_exception
            result = self._record.terminal_result
        if terminal_exception is not None:
            raise terminal_exception
        assert result is not None
        return result

    def cancel(self) -> None:
        """Request cancellation. Idempotent; never blocks - use `wait()`
        for confirmed termination."""
        self._manager.cancel(self._record)

    def close(self) -> None:
        """Cancel an unfinished session and wait up to the manager's
        bounded close-wait interval. Idempotent; does not raise on
        timeout, mirroring `JobHandle.close()`."""
        self._manager.close_session(self._record)
        with contextlib.suppress(Exception):
            self.wait(timeout=self._manager.close_wait_seconds)
        if self._on_dispose is not None:
            with contextlib.suppress(Exception):
                self._on_dispose()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def _build_config_overlay(request: AuthorizationRequest) -> dict[str, object]:
    """Build the `_config` overlay scoping `OAuthRedirectURL`/
    `OAuthListenAddress` to this one session's RC calls, matching how the
    fork already implements per-flow overrides (`fs/rc/context.go`).

    Omits `OAuthRedirectURL` entirely when `public_callback_url` is
    `None` (the local-direct case - see `AuthorizationRequest`'s
    docstring): overriding it would break rclone's own built-in shared
    client_id, which is only registered with the provider for its own
    default redirect URI.
    """
    overlay: dict[str, object] = {}
    if request.public_callback_url is not None:
        overlay["OAuthRedirectURL"] = request.public_callback_url
    if request.private_listen_addr is not None:
        overlay["OAuthListenAddress"] = request.private_listen_addr
    return overlay


def _translate_auth_url(
    request: AuthorizationRequest, private_auth_url: str
) -> tuple[str, str, str]:
    """Translate `config/oauthstatus`'s internal `authUrl` (always
    `http://<listener>/auth?state=...` - see `rc/auth.py`'s module
    docstring) into `(public_url, private_base_url, oauth_state)`.

    `public_url` is `public_callback_url` with `/auth?state=...`
    appended, matching the relay's two public path shapes - or, when
    `public_callback_url` is `None` (local-direct mode), `private_auth_url`
    itself, meant to be opened directly by a browser on the same machine.
    """
    parsed = urlsplit(private_auth_url)
    states = parse_qs(parsed.query).get("state")
    if not states or not states[0]:
        raise ValueError(f"authUrl {private_auth_url!r} has no state query parameter")
    state = states[0]
    private_base_url = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    if request.public_callback_url is None:
        return private_auth_url, private_base_url, state
    public_url = f"{request.public_callback_url.rstrip('/')}/auth?state={state}"
    return public_url, private_base_url, state


def start_active(
    record: _SessionRecord,
    auth_client: RcAuthClient,
    build_result: Callable[[str], AuthorizationResult],
    cleanup_on_failure: Callable[[_SessionRecord], None],
    on_settled: Callable[[_SessionRecord], None],
) -> None:
    """Spawn the driver thread for `record`, which the caller has already
    admitted (transitioned to `STARTING`)."""
    threading.Thread(
        target=_run_driver,
        args=(record, auth_client, build_result, cleanup_on_failure, on_settled),
        daemon=True,
        name=f"rclone-kit-authz-driver-{record.id}",
    ).start()


def _run_driver(
    record: _SessionRecord,
    auth_client: RcAuthClient,
    build_result: Callable[[str], AuthorizationResult],
    cleanup_on_failure: Callable[[_SessionRecord], None],
    on_settled: Callable[[_SessionRecord], None],
) -> None:
    watcher_thread: threading.Thread | None = None

    def start_watcher() -> None:
        nonlocal watcher_thread
        watcher_thread = threading.Thread(
            target=_run_watcher,
            args=(record, auth_client),
            daemon=True,
            name=f"rclone-kit-authz-watcher-{record.id}",
        )
        watcher_thread.start()

    config_overlay = _build_config_overlay(record.request)
    start, continue_ = build_call_functions(
        auth_client, record.request, config_overlay=config_overlay
    )

    try:
        drive(record.request.remote_name, start, continue_, on_before_blocking_call=start_watcher)
    except AuthorizationRejectedError as error:
        with record.condition:
            expired = record.expire_requested
            cancelled = record.cancel_requested
        if (expired or cancelled) and _OAUTH_CANCELLED_MARKER in str(error):
            if expired:
                record.mark_expired()
            else:
                record.mark_cancelled()
        else:
            record.mark_failed(error)
        cleanup_on_failure(record)
    except AuthorizationError as error:
        record.mark_failed(error)
        cleanup_on_failure(record)
    except Exception as error:  # defensive: an unexpected native/RC surprise
        logger.exception("unexpected error driving authorization session %s", record.id)
        record.mark_failed(error)
        cleanup_on_failure(record)
    else:
        try:
            result = build_result(record.request.remote_name)
        except Exception as error:
            # rclone already saved a validly authorized remote - do not
            # delete it just because reading it back failed.
            record.mark_failed(error)
        else:
            record.mark_succeeded(result)
    finally:
        record.watcher_stop.set()
        if watcher_thread is not None:
            watcher_thread.join(timeout=_WATCHER_JOIN_TIMEOUT_SECONDS)
        on_settled(record)


def _run_watcher(record: _SessionRecord, auth_client: RcAuthClient) -> None:
    """Poll `config/oauthstatus` until it reports `"running"` and captures
    the OAuth URL, or the driver thread signals completion - whichever
    comes first. A flow that fails before binding the listener (a bad
    `client_id`, for instance) never reaches `"running"` at all, so this
    loop must stop on `watcher_stop` rather than wait for a state that
    isn't coming.
    """
    while not record.watcher_stop.is_set():
        try:
            status = auth_client.oauth_status()
        except Exception:
            logger.warning(
                "transient error polling config/oauthstatus for session %s",
                record.id,
                exc_info=True,
            )
            record.watcher_stop.wait(_WATCHER_POLL_INTERVAL_SECONDS)
            continue

        if status.running and status.auth_url:
            try:
                public_url, private_base_url, oauth_state = _translate_auth_url(
                    record.request, status.auth_url
                )
            except ValueError:
                logger.warning("could not parse authUrl for session %s", record.id, exc_info=True)
                return
            record._advance(
                AuthorizationStatus.WAITING_FOR_USER,
                authorization_url=public_url,
                private_base_url=private_base_url,
                oauth_state=oauth_state,
            )
            return

        record.watcher_stop.wait(_WATCHER_POLL_INTERVAL_SECONDS)
