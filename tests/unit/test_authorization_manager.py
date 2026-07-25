"""Unit tests for `AuthorizationManager`: single-flight queue admission,
name-collision protection, caps, cancellation/expiry of queued and active
sessions, cleanup-on-failure, and relay routing.

Uses a scripted fake `RcAuthClient` whose blocking continuation call
(modeling `*oauth-do`'s `configSetup()` wait) is driven by a per-remote
`threading.Event` the test controls - real `threading.Thread`/`Condition`/
`Timer` machinery throughout, not a simulated clock, mirroring
`test_job.py`'s approach for `_JobMonitor`. Every wait uses a generous
timeout so a real regression fails fast rather than hanging.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rclone_kit.authorization.exceptions import (
    AuthorizationCancelledError,
    AuthorizationExpiredError,
    AuthorizationQueueFullError,
    AuthorizationRelayError,
    AuthorizationRemoteNameConflictError,
)
from rclone_kit.authorization.manager import AuthorizationManager
from rclone_kit.authorization.types import (
    AuthorizationRequest,
    AuthorizationStatus,
    RelayRequest,
    RemoteConflictPolicy,
)
from rclone_kit.rc.auth import ConfigStep, OAuthStatus
from rclone_kit.rc.errors import RcCallError

_WAIT_TIMEOUT = 5.0
_CANCELLED_MARKER_ERROR = RcCallError(
    "config/create",
    500,
    {"error": "config failed to refresh token: oauth authentication was cancelled"},
)


class ScriptedAuthClient:
    """A fake `RcAuthClient` where the call that would block inside
    rclone's OAuth webserver wait (answering `config_is_local`/
    `config_refresh_token`) instead blocks on a per-remote
    `threading.Event`, released by the test via `release()` or by
    `oauth_stop()` (modeling cancellation/expiry)."""

    def __init__(self) -> None:
        self.listremotes_result: tuple[str, ...] = ()
        self.created: list[str] = []
        self.updated: list[str] = []
        self.deleted: list[str] = []
        self._auth_url_by_name: dict[str, str] = {}
        self._lock = threading.Lock()
        self._gates: dict[str, threading.Event] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._status = OAuthStatus(running=False, auth_url=None)

    def set_auth_url(self, name: str, auth_url: str) -> None:
        self._auth_url_by_name[name] = auth_url

    def release(self, name: str) -> None:
        self._gates.setdefault(name, threading.Event()).set()

    def _block_until_released_or_cancelled(self, name: str) -> ConfigStep:
        gate = self._gates.setdefault(name, threading.Event())
        cancel_flag = self._cancel_flags.setdefault(name, threading.Event())
        auth_url = self._auth_url_by_name.get(name, f"http://127.0.0.1:53682/auth?state={name}")
        with self._lock:
            self._status = OAuthStatus(running=True, auth_url=auth_url)
        while not gate.wait(timeout=0.02):
            if cancel_flag.is_set():
                with self._lock:
                    self._status = OAuthStatus(running=False, auth_url=None)
                raise _CANCELLED_MARKER_ERROR
        with self._lock:
            self._status = OAuthStatus(running=False, auth_url=None)
        return ConfigStep(state="", option_name=None, error=None)

    def create(self, name, backend, parameters, *, config_overlay=None):  # noqa: ARG002
        self.created.append(name)
        return ConfigStep(state=f"s:{name}", option_name="config_is_local", error=None)

    def create_continue(self, name, backend, state, result, parameters, *, config_overlay=None):  # noqa: ARG002
        assert result == "true"
        return self._block_until_released_or_cancelled(name)

    def update(self, name, parameters, *, config_overlay=None):  # noqa: ARG002
        self.updated.append(name)
        return ConfigStep(state=f"s:{name}", option_name="config_is_local", error=None)

    def update_continue(self, name, state, result, parameters, *, config_overlay=None):  # noqa: ARG002
        assert result == "true"
        return self._block_until_released_or_cancelled(name)

    def delete(self, name):
        self.deleted.append(name)

    def listremotes(self):
        return self.listremotes_result

    def oauth_status(self):
        with self._lock:
            return self._status

    def oauth_stop(self):
        with self._lock:
            if not self._status.running:
                raise RcCallError(
                    "config/oauthstop", 500, {"error": "no oauth authentication is in progress"}
                )
        for name, flag in self._cancel_flags.items():
            gate = self._gates.get(name)
            if gate is not None and not gate.is_set():
                flag.set()


class FakeRcClient:
    """A fake `RcCallable` satisfying only `rclonekit/configshow`, which is
    all `AuthorizationManager._build_result`/`_cleanup_on_failure` need.
    `text_by_remote` lets a test simulate a remote that already holds a
    saved token, defaulting to one that doesn't."""

    def __init__(self) -> None:
        self.text_by_remote: dict[str, str] = {}

    def call(self, method: str, **params: object) -> dict:
        assert method == "rclonekit/configshow"
        remote = params["remote"]
        assert isinstance(remote, str)
        text = self.text_by_remote.get(remote, f"[{remote}]\ntype = drive\n")
        return {"text": text}


def _manager(
    auth_client: ScriptedAuthClient | None = None,
    *,
    rc_client: FakeRcClient | None = None,
    pending_cap: int = 100,
    per_owner_cap: int | None = None,
) -> tuple[AuthorizationManager, ScriptedAuthClient]:
    client = auth_client or ScriptedAuthClient()
    manager = AuthorizationManager(
        client, rc_client or FakeRcClient(), pending_cap=pending_cap, per_owner_cap=per_owner_cap
    )
    return manager, client


def _request(remote_name: str = "gdrive", **overrides: object) -> AuthorizationRequest:
    fields: dict[str, object] = {
        "remote_name": remote_name,
        "backend": "drive",
        "public_callback_url": "https://example.com/oauth/rclone/callback",
        "expires_in": timedelta(minutes=10),
    }
    fields.update(overrides)
    return AuthorizationRequest(**fields)  # type: ignore[arg-type]


def _wait_until(predicate, timeout: float = _WAIT_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


class TestImmediateAdmission:
    def test_start_admits_immediately_when_idle(self) -> None:
        manager, client = _manager()

        session = manager.start(_request("gdrive"))
        _wait_until(lambda: "gdrive" in client.created)

        assert session.status in (
            AuthorizationStatus.STARTING,
            AuthorizationStatus.WAITING_FOR_USER,
        )
        client.release("gdrive")
        result = session.wait(timeout=_WAIT_TIMEOUT)
        assert result.remote_name == "gdrive"

    def test_authorization_url_is_the_public_callback_plus_auth_and_state(self) -> None:
        manager, client = _manager()

        session = manager.start(_request("gdrive"))
        url = session.authorization_url

        assert url == "https://example.com/oauth/rclone/callback/auth?state=gdrive"
        client.release("gdrive")
        session.wait(timeout=_WAIT_TIMEOUT)

    def test_local_direct_mode_authorization_url_is_the_raw_internal_url(self) -> None:
        manager, client = _manager()
        client.set_auth_url("gdrive", "http://127.0.0.1:53682/auth?state=gdrive")

        session = manager.start(_request("gdrive", public_callback_url=None))
        url = session.authorization_url

        assert url == "http://127.0.0.1:53682/auth?state=gdrive"
        client.release("gdrive")
        session.wait(timeout=_WAIT_TIMEOUT)


class TestNameCollision:
    def test_reject_policy_raises_before_any_rclone_call(self) -> None:
        client = ScriptedAuthClient()
        client.listremotes_result = ("gdrive",)
        manager, client = _manager(client)

        with pytest.raises(AuthorizationRemoteNameConflictError):
            manager.start(_request("gdrive"))
        assert client.created == []

    def test_reconnect_policy_bypasses_the_collision_check(self) -> None:
        client = ScriptedAuthClient()
        client.listremotes_result = ("gdrive",)
        manager, client = _manager(client)

        session = manager.start(_request("gdrive", on_conflict=RemoteConflictPolicy.RECONNECT))
        _wait_until(lambda: "gdrive" in client.updated)
        client.release("gdrive")
        session.wait(timeout=_WAIT_TIMEOUT)

        assert client.updated == ["gdrive"]
        assert client.created == []


class TestSingleFlightSerialization:
    def test_second_session_stays_queued_until_the_first_settles(self) -> None:
        manager, client = _manager()

        first = manager.start(_request("a"))
        _wait_until(lambda: "a" in client.created)
        first.authorization_url  # noqa: B018 - block until admitted/WAITING_FOR_USER

        second = manager.start(_request("b"))
        time.sleep(0.1)  # give a wrongly-eager admission a chance to happen

        assert second.status is AuthorizationStatus.QUEUED
        assert client.created == ["a"]

        client.release("a")
        first.wait(timeout=_WAIT_TIMEOUT)
        _wait_until(lambda: "b" in client.created)
        client.release("b")
        second.wait(timeout=_WAIT_TIMEOUT)

        assert client.created == ["a", "b"]

    def test_second_session_never_observes_the_first_sessions_oauth_status(self) -> None:
        manager, client = _manager()
        client.set_auth_url("a", "http://127.0.0.1:53682/auth?state=a")
        client.set_auth_url("b", "http://127.0.0.1:53682/auth?state=b")

        first = manager.start(_request("a"))
        assert first.authorization_url.endswith("state=a")

        second = manager.start(_request("b"))
        # the second session must not reach a point where oauth_status()
        # (rclone's process-wide global) could report its own URL while
        # the first is still active
        assert second.status is AuthorizationStatus.QUEUED

        client.release("a")
        first.wait(timeout=_WAIT_TIMEOUT)
        assert second.authorization_url.endswith("state=b")
        client.release("b")
        second.wait(timeout=_WAIT_TIMEOUT)


class TestQueueCap:
    def test_start_beyond_the_pending_cap_is_rejected(self) -> None:
        manager, client = _manager(pending_cap=0)

        first = manager.start(_request("a"))
        _wait_until(lambda: "a" in client.created)

        with pytest.raises(AuthorizationQueueFullError):
            manager.start(_request("b"))

        client.release("a")
        first.wait(timeout=_WAIT_TIMEOUT)


class TestCancel:
    def test_cancel_a_queued_session_settles_cancelled_without_touching_rclone(self) -> None:
        manager, client = _manager()
        first = manager.start(_request("a"))
        _wait_until(lambda: "a" in client.created)
        second = manager.start(_request("b"))
        _wait_until(lambda: second.status is AuthorizationStatus.QUEUED)

        second.cancel()

        with pytest.raises(AuthorizationCancelledError):
            second.wait(timeout=_WAIT_TIMEOUT)
        assert "b" not in client.created

        client.release("a")
        first.wait(timeout=_WAIT_TIMEOUT)

    def test_cancel_an_active_session_with_no_saved_token_cleans_up(self) -> None:
        manager, client = _manager()
        session = manager.start(_request("a"))
        _wait_until(lambda: session.status is AuthorizationStatus.WAITING_FOR_USER)

        session.cancel()

        with pytest.raises(AuthorizationCancelledError):
            session.wait(timeout=_WAIT_TIMEOUT)
        assert client.deleted == ["a"]

    def test_cancelling_the_active_session_promotes_the_next_queued_one(self) -> None:
        manager, client = _manager()
        first = manager.start(_request("a"))
        _wait_until(lambda: "a" in client.created)
        second = manager.start(_request("b"))
        _wait_until(lambda: second.status is AuthorizationStatus.QUEUED)

        first.cancel()
        with pytest.raises(AuthorizationCancelledError):
            first.wait(timeout=_WAIT_TIMEOUT)

        _wait_until(lambda: "b" in client.created)
        client.release("b")
        second.wait(timeout=_WAIT_TIMEOUT)


class TestCleanupOnFailure:
    """A failure/cancellation/expiry can happen *after* rclone already
    saved a real token (e.g. a post-OAuth follow-up question this driver
    doesn't recognize) - cleanup must never destroy that credential, only
    a section that never got one."""

    def test_no_saved_token_is_deleted(self) -> None:
        manager, client = _manager()
        session = manager.start(_request("a"))
        _wait_until(lambda: session.status is AuthorizationStatus.WAITING_FOR_USER)

        session.cancel()

        with pytest.raises(AuthorizationCancelledError):
            session.wait(timeout=_WAIT_TIMEOUT)
        assert client.deleted == ["a"]

    def test_a_saved_token_is_never_deleted(self) -> None:
        rc_client = FakeRcClient()
        rc_client.text_by_remote["a"] = '[a]\ntype = drive\ntoken = {"access_token":"real"}\n'
        manager, client = _manager(rc_client=rc_client)
        session = manager.start(_request("a"))
        _wait_until(lambda: session.status is AuthorizationStatus.WAITING_FOR_USER)

        session.cancel()

        with pytest.raises(AuthorizationCancelledError):
            session.wait(timeout=_WAIT_TIMEOUT)
        assert client.deleted == []

    def test_reconnect_policy_never_deletes_regardless_of_token(self) -> None:
        manager, client = _manager()
        session = manager.start(_request("a", on_conflict=RemoteConflictPolicy.RECONNECT))
        _wait_until(lambda: session.status is AuthorizationStatus.WAITING_FOR_USER)

        session.cancel()

        with pytest.raises(AuthorizationCancelledError):
            session.wait(timeout=_WAIT_TIMEOUT)
        assert client.deleted == []


class TestCloseSession:
    def test_close_a_queued_session_settles_closed_without_touching_rclone(self) -> None:
        manager, client = _manager()
        first = manager.start(_request("a"))
        _wait_until(lambda: "a" in client.created)
        second = manager.start(_request("b"))
        _wait_until(lambda: second.status is AuthorizationStatus.QUEUED)

        second.close()

        assert second.status is AuthorizationStatus.CLOSED
        assert "b" not in client.created

        client.release("a")
        first.wait(timeout=_WAIT_TIMEOUT)

    def test_close_an_active_session_settles_cancelled_not_closed(self) -> None:
        manager, _client = _manager()
        session = manager.start(_request("a"))
        _wait_until(lambda: session.status is AuthorizationStatus.WAITING_FOR_USER)

        session.close()

        assert session.status is AuthorizationStatus.CANCELLED


class TestExpiry:
    def test_active_session_expires_after_its_deadline(self) -> None:
        manager, client = _manager()
        session = manager.start(_request("a", expires_in=timedelta(milliseconds=50)))

        with pytest.raises(AuthorizationExpiredError):
            session.wait(timeout=_WAIT_TIMEOUT)
        assert client.deleted == ["a"]

    def test_queued_session_expires_without_ever_touching_rclone(self) -> None:
        manager, client = _manager()
        first = manager.start(_request("a"))
        _wait_until(lambda: "a" in client.created)
        second = manager.start(_request("b", expires_in=timedelta(milliseconds=50)))

        with pytest.raises(AuthorizationExpiredError):
            second.wait(timeout=_WAIT_TIMEOUT)
        assert "b" not in client.created

        client.release("a")
        first.wait(timeout=_WAIT_TIMEOUT)


class _PrivateListenerHandler(BaseHTTPRequestHandler):
    """A minimal stand-in for rclone's private OAuth listener."""

    received_paths: list[str] = []  # noqa: RUF012 - reset per test via clear()

    def log_message(self, format: str, *args: object) -> None:  # quiet the test output
        pass

    def do_GET(self) -> None:
        self.received_paths.append(self.path)
        if self.path.startswith("/auth"):
            self.send_response(307)
            self.send_header("Location", "https://provider.example.com/authorize")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


@pytest.fixture
def private_listener() -> Iterator[tuple[str, list[str]]]:
    _PrivateListenerHandler.received_paths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PrivateListenerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}", _PrivateListenerHandler.received_paths
    finally:
        server.shutdown()
        thread.join(timeout=_WAIT_TIMEOUT)


class TestRelay:
    def test_relay_forwards_to_the_active_sessions_private_listener(
        self, private_listener: tuple[str, list[str]]
    ) -> None:
        addr, received_paths = private_listener
        manager, client = _manager()
        client.set_auth_url("a", f"http://{addr}/auth?state=a")
        session = manager.start(_request("a"))
        assert session.authorization_url.endswith("state=a")

        response = manager.relay(
            RelayRequest(path="/oauth/rclone/callback/auth", raw_query=b"state=a")
        )

        assert response.status_code == 307
        assert received_paths == ["/auth?state=a"]
        assert session.status is AuthorizationStatus.WAITING_FOR_USER

        client.release("a")
        session.wait(timeout=_WAIT_TIMEOUT)

    def test_relay_to_callback_path_marks_completing(
        self, private_listener: tuple[str, list[str]]
    ) -> None:
        addr, _received_paths = private_listener
        manager, client = _manager()
        client.set_auth_url("a", f"http://{addr}/auth?state=a")
        session = manager.start(_request("a"))
        assert session.authorization_url.endswith("state=a")

        manager.relay(RelayRequest(path="/oauth/rclone/callback", raw_query=b"state=a&code=xyz"))

        assert session.status is AuthorizationStatus.COMPLETING
        client.release("a")
        session.wait(timeout=_WAIT_TIMEOUT)

    def test_relay_with_no_active_session_raises(self) -> None:
        manager, _client = _manager()

        with pytest.raises(AuthorizationRelayError, match="no active"):
            manager.relay(RelayRequest(path="/oauth/rclone/callback/auth", raw_query=b"state=x"))

    def test_relay_with_mismatched_state_raises(self) -> None:
        manager, client = _manager()
        session = manager.start(_request("a"))
        assert session.authorization_url.endswith("state=a")

        with pytest.raises(AuthorizationRelayError, match="state"):
            manager.relay(
                RelayRequest(path="/oauth/rclone/callback/auth", raw_query=b"state=wrong")
            )

        client.release("a")
        session.wait(timeout=_WAIT_TIMEOUT)
