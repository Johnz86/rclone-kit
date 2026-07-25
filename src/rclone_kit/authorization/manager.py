"""`AuthorizationManager`: the process-wide (really: per-`RcloneRuntime`)
single-flight queue for authorization sessions
(`docs/rclone_authorization_design.md`, "Central architectural constraint"
and "Session routing and concurrency").

Owns the queue, the `WeakKeyDictionary` runtime keying, and relay routing -
nothing about HTTP transport (`relay.py`) or the config state machine
(`state_driver.py`/`session.py`). At most one session is ever
`STARTING`/`WAITING_FOR_USER`/`COMPLETING` at a time, because rclone's own
OAuth flow state (`oauthCancelFn`/`oauthURL` in
`native/rclone/lib/oauthutil/oauthutil.go`) is a process-wide global, not
per-session - see `rc/auth.py`'s module docstring and the design doc.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from rclone_kit.authorization import relay as relay_module
from rclone_kit.authorization.exceptions import (
    AuthorizationQueueFullError,
    AuthorizationRelayError,
    AuthorizationRemoteNameConflictError,
)
from rclone_kit.authorization.session import AuthorizationSession, _SessionRecord, start_active
from rclone_kit.authorization.types import (
    AuthorizationResult,
    AuthorizationStatus,
    RemoteConflictPolicy,
)
from rclone_kit.config import Config
from rclone_kit.operations.config_ops import fetch_config_show_embedded
from rclone_kit.rc.auth import RcloneRcAuthClient
from rclone_kit.rc.client import RcClient

if TYPE_CHECKING:
    from rclone_kit.authorization.types import AuthorizationRequest, RelayRequest, RelayResponse
    from rclone_kit.native.runtime import RcloneRuntime
    from rclone_kit.rc.auth import RcAuthClient
    from rclone_kit.rc.client import RcCallable

_DEFAULT_PENDING_CAP = 100
_DEFAULT_CLOSE_WAIT_SECONDS = 5.0
_ACTIVE_RELAY_STATUSES = (AuthorizationStatus.WAITING_FOR_USER, AuthorizationStatus.COMPLETING)


def _has_token(remote_config_text: str) -> bool:
    """`remote_config_text` is `rclonekit/configshow`'s single-remote
    output, scoped to exactly one `[name]` section (or none, for a remote
    that doesn't exist). `oauthutil.PutToken()` always stores a completed
    OAuth token under the same key (`config.ConfigToken = "token"`)
    regardless of backend, so this is a backend-neutral way to tell "this
    section holds a real, usable credential" from "this section is
    broken/incomplete/absent"."""
    parsed = Config(remote_config_text).parse()
    return any("token" in section.data for section in parsed.sections.values())


class AuthorizationManager:
    """Drives authorization sessions against one shared `RcloneRuntime`.

    Never construct directly for production use - `for_runtime()` is the
    one construction point, mirroring `shared_runtime()`'s own "construct
    exactly once, share everywhere" pattern, so every `Rclone` client
    built on the same runtime resolves to the same manager and therefore
    the same single-flight queue.
    """

    _registry: WeakKeyDictionary[object, AuthorizationManager] = WeakKeyDictionary()
    _registry_lock = threading.Lock()

    def __init__(
        self,
        auth_client: RcAuthClient,
        rc_client: RcCallable,
        *,
        pending_cap: int = _DEFAULT_PENDING_CAP,
        per_owner_cap: int | None = None,
        close_wait_seconds: float = _DEFAULT_CLOSE_WAIT_SECONDS,
    ) -> None:
        """`auth_client` and `rc_client` are both constructed by the
        caller (mirroring `_JobMonitor(RcloneRcJobClient(...))`'s own
        shape) rather than derived internally, so this class stays
        testable against fakes without a real `RcClient`/`RcloneRuntime`.
        `for_runtime()` is production code's one construction point."""
        self._rc_client = rc_client
        self._auth_client = auth_client
        self._pending_cap = pending_cap
        self._per_owner_cap = per_owner_cap
        self.close_wait_seconds = close_wait_seconds
        # Reentrant: `_arm_timer` acquires this lock itself (to bump a
        # record's generation counter atomically - see its docstring) but
        # is also called from within an already-locked block in `start()`.
        self._lock = threading.RLock()
        self._active: _SessionRecord | None = None
        self._pending: deque[_SessionRecord] = deque()

    @classmethod
    def for_runtime(cls, runtime: RcloneRuntime) -> AuthorizationManager:
        """Return the manager shared by every caller for `runtime`,
        creating it on the first call. Every call after the first ignores
        any per-call construction arguments, matching
        `shared_runtime()`'s own once-only semantics."""
        with cls._registry_lock:
            manager = cls._registry.get(runtime)
            if manager is None:
                rc_client = RcClient(runtime)
                manager = cls(RcloneRcAuthClient(rc_client), rc_client)
                cls._registry[runtime] = manager
            return manager

    def start(
        self, request: AuthorizationRequest, *, owner: str | None = None
    ) -> AuthorizationSession:
        """Enqueue `request` and return its session immediately.

        Raises `AuthorizationRemoteNameConflictError` before ever calling
        rclone if `request.remote_name` already exists and
        `request.on_conflict` is `RemoteConflictPolicy.REJECT`. Raises
        `AuthorizationQueueFullError` before enqueueing if the pending
        queue (globally, or for `owner` specifically) is already at its
        configured cap.
        """
        if request.on_conflict is RemoteConflictPolicy.REJECT:
            existing = self._auth_client.listremotes()
            if request.remote_name in existing:
                raise AuthorizationRemoteNameConflictError(request.remote_name)

        enqueued_at = datetime.now(UTC)
        record = _SessionRecord(
            id=str(uuid.uuid4()),
            request=request,
            owner=owner,
            expires_at=enqueued_at + request.expires_in,
        )

        admit_now = False
        with self._lock:
            if self._active is None:
                self._active = record
                admit_now = True
            else:
                # Caps only ever apply to *queueing* - a request that can
                # be admitted immediately never touches the pending queue
                # at all, so pending_cap=0 still allows one active session.
                if len(self._pending) >= self._pending_cap:
                    raise AuthorizationQueueFullError(request.remote_name, self._pending_cap)
                if owner is not None and self._per_owner_cap is not None:
                    owned = sum(1 for r in self._pending if r.owner == owner)
                    if self._active.owner == owner:
                        owned += 1
                    if owned >= self._per_owner_cap:
                        raise AuthorizationQueueFullError(request.remote_name, self._per_owner_cap)
                self._pending.append(record)
                self._arm_timer(record, remaining_seconds=request.expires_in.total_seconds())

        if admit_now:
            self._admit(record)
        return AuthorizationSession(self, record)

    def relay(self, request: RelayRequest) -> RelayResponse:
        """Translate and forward one public-callback request to the one
        currently-active session's private OAuth listener.

        Extracts `state` only to confirm the request targets the active
        session and that it matches what that session's
        `authorization_url` reported; never selects a private address
        from anything in `request` itself.
        """
        with self._lock:
            active = self._active

        if active is None:
            raise AuthorizationRelayError("no active authorization session")

        with active.condition:
            if active.status not in _ACTIVE_RELAY_STATUSES:
                raise AuthorizationRelayError("no active authorization session")
            private_base_url = active.private_base_url
            oauth_state = active.oauth_state

        if private_base_url is None or oauth_state is None:
            raise AuthorizationRelayError("authorization session has no listener address yet")

        if relay_module.extract_state(request.raw_query) != oauth_state:
            raise AuthorizationRelayError("callback state does not match the active session")

        if not relay_module.is_auth_path(request.path):
            active._advance(AuthorizationStatus.COMPLETING)

        return relay_module.forward(private_base_url, request)

    def cancel(self, record: _SessionRecord) -> None:
        """Idempotent; never blocks on rclone - dispatches `oauthstop` on
        a background thread for an active session, matching
        `_JobMonitor.request_cancel`'s own async-dispatch rationale."""
        with record.condition:
            if record.status.is_terminal:
                return

        with self._lock:
            if record in self._pending:
                self._pending.remove(record)
                self._cancel_timer(record)
                record.mark_cancelled()
                return
            is_active = record is self._active

        if not is_active:
            return

        with record.condition:
            if record.status.is_terminal:
                return
            record.cancel_requested = True
        threading.Thread(
            target=self._dispatch_cancel,
            daemon=True,
            name=f"rclone-kit-authz-cancel-{record.id}",
        ).start()

    def close_session(self, record: _SessionRecord) -> None:
        """A still-`QUEUED` session is dropped and settled `CLOSED`
        directly - nothing at the rclone level was ever running to
        cancel. Any other unfinished session is cancelled, settling
        `CANCELLED`."""
        with record.condition:
            if record.status.is_terminal:
                return
            was_queued = record.status is AuthorizationStatus.QUEUED

        if not was_queued:
            self.cancel(record)
            return

        with self._lock, contextlib.suppress(ValueError):
            self._pending.remove(record)
        self._cancel_timer(record)
        record.mark_closed()

    def _dispatch_cancel(self) -> None:
        with contextlib.suppress(Exception):
            self._auth_client.oauth_stop()

    def _admit(self, record: _SessionRecord) -> None:
        # Cancel the queue-wait timer armed at enqueue time before arming
        # the consent-window one below - otherwise both would be live at
        # once. `_arm_timer`'s generation counter makes this race-safe even
        # when the old timer has already fired by the time this runs (its
        # callback checks its own generation against the current one).
        self._cancel_timer(record)
        admitted_at = datetime.now(UTC)
        expires_at = admitted_at + record.request.expires_in
        record._advance(AuthorizationStatus.STARTING, expires_at=expires_at)
        self._arm_timer(record, remaining_seconds=record.request.expires_in.total_seconds())
        start_active(
            record,
            self._auth_client,
            self._build_result,
            self._cleanup_on_failure,
            self._on_settled,
        )

    def _promote_next(self) -> None:
        while True:
            with self._lock:
                if self._active is not None or not self._pending:
                    return
                candidate = self._pending.popleft()
                if candidate.status.is_terminal:
                    continue  # settled while queued (cancelled/expired) - skip it
                self._active = candidate
            self._admit(candidate)
            return

    def _on_settled(self, record: _SessionRecord) -> None:
        self._cancel_timer(record)
        with self._lock:
            if self._active is record:
                self._active = None
        self._promote_next()

    def _on_deadline(self, record: _SessionRecord, generation: int) -> None:
        with self._lock:
            if record.status.is_terminal or generation != record.generation:
                # Either already settled, or this is a stale timer from a
                # phase (queued vs. active) this record has since left -
                # `_admit`'s re-arm raced with this firing. A no-op either
                # way; the live timer for the current phase is unaffected.
                return
            is_active = record is self._active
            if not is_active:
                with contextlib.suppress(ValueError):
                    self._pending.remove(record)

        if is_active:
            with record.condition:
                if record.status.is_terminal:
                    return
                record.expire_requested = True
            with contextlib.suppress(Exception):
                self._auth_client.oauth_stop()
        else:
            record.mark_expired()
            self._promote_next()

    def _arm_timer(self, record: _SessionRecord, *, remaining_seconds: float) -> None:
        with self._lock:
            record.generation += 1
            generation = record.generation
        timer = threading.Timer(
            max(remaining_seconds, 0.0), self._on_deadline, args=(record, generation)
        )
        timer.daemon = True
        record.timer = timer
        timer.start()

    @staticmethod
    def _cancel_timer(record: _SessionRecord) -> None:
        if record.timer is not None:
            record.timer.cancel()

    def _build_result(self, remote_name: str) -> AuthorizationResult:
        text = fetch_config_show_embedded(self._rc_client, remote=remote_name)
        return AuthorizationResult(remote_name=remote_name, config=Config(text))

    def _cleanup_on_failure(self, record: _SessionRecord) -> None:
        """Delete the section a failed/cancelled/expired session may have
        left behind - but never one that actually holds a token: rclone
        saves progress (`SaveConfig()`) after *every* successful
        `BackendConfig` step, including one that stops on a question this
        driver doesn't know how to answer (e.g. a backend's post-OAuth
        follow-up prompt) - so a real, usable credential can already be
        saved by the time this runs. Deleting that would silently throw
        away a token the user just finished granting, which is worse than
        leaving a section behind for a caller to notice and retry
        (`RemoteConflictPolicy.RECONNECT`) or delete themselves.
        """
        if record.request.on_conflict is RemoteConflictPolicy.RECONNECT:
            return
        remote_name = record.request.remote_name
        with contextlib.suppress(Exception):
            if _has_token(fetch_config_show_embedded(self._rc_client, remote=remote_name)):
                return
            self._auth_client.delete(remote_name)
