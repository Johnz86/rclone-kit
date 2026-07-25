"""Unit tests for `AuthorizationSession`'s handle behavior:
properties, `wait()`/`authorization_url`'s blocking semantics, and
`cancel()`/`close()` delegating to the manager. Drives a manually
constructed `_SessionRecord` directly (no real driver/watcher threads),
mirroring `test_job.py`'s style for `JobHandle`.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from rclone_kit.authorization.exceptions import AuthorizationRejectedError
from rclone_kit.authorization.session import (
    AuthorizationSession,
    _build_config_overlay,
    _SessionRecord,
    _translate_auth_url,
)
from rclone_kit.authorization.types import (
    AuthorizationRequest,
    AuthorizationResult,
    AuthorizationStatus,
)
from rclone_kit.config import Config

_WAIT_TIMEOUT = 2.0

_REQUEST = AuthorizationRequest(
    remote_name="gdrive",
    backend="drive",
    public_callback_url="https://example.com/oauth/rclone/callback",
)

_LOCAL_DIRECT_REQUEST = AuthorizationRequest(remote_name="gdrive", backend="drive")

_PRIVATE_AUTH_URL = "http://127.0.0.1:53682/auth?state=s1"


class TestBuildConfigOverlay:
    def test_relay_mode_sets_oauth_redirect_url(self) -> None:
        assert _build_config_overlay(_REQUEST) == {
            "OAuthRedirectURL": "https://example.com/oauth/rclone/callback"
        }

    def test_local_direct_mode_omits_oauth_redirect_url(self) -> None:
        assert _build_config_overlay(_LOCAL_DIRECT_REQUEST) == {}

    def test_private_listen_addr_is_included_in_both_modes(self) -> None:
        request = AuthorizationRequest(
            remote_name="gdrive", backend="drive", private_listen_addr="127.0.0.1:0"
        )

        assert _build_config_overlay(request) == {"OAuthListenAddress": "127.0.0.1:0"}


class TestTranslateAuthUrl:
    def test_relay_mode_builds_the_public_callback_url(self) -> None:
        public_url, private_base_url, state = _translate_auth_url(_REQUEST, _PRIVATE_AUTH_URL)

        assert public_url == "https://example.com/oauth/rclone/callback/auth?state=s1"
        assert private_base_url == "http://127.0.0.1:53682"
        assert state == "s1"

    def test_local_direct_mode_returns_the_private_url_unchanged(self) -> None:
        public_url, private_base_url, state = _translate_auth_url(
            _LOCAL_DIRECT_REQUEST, _PRIVATE_AUTH_URL
        )

        assert public_url == _PRIVATE_AUTH_URL
        assert private_base_url == "http://127.0.0.1:53682"
        assert state == "s1"

    def test_missing_state_query_parameter_raises(self) -> None:
        with pytest.raises(ValueError, match="state"):
            _translate_auth_url(_REQUEST, "http://127.0.0.1:53682/auth")


class FakeManager:
    def __init__(self) -> None:
        self.close_wait_seconds = 2.0
        self.cancel_calls: list[_SessionRecord] = []
        self.close_calls: list[_SessionRecord] = []

    def cancel(self, record: _SessionRecord) -> None:
        self.cancel_calls.append(record)

    def close_session(self, record: _SessionRecord) -> None:
        self.close_calls.append(record)


def _record(*, expires_in: timedelta = timedelta(minutes=10)) -> _SessionRecord:
    return _SessionRecord(
        id="session-1",
        request=_REQUEST,
        owner=None,
        expires_at=datetime.now(UTC) + expires_in,
    )


class TestProperties:
    def test_id_status_and_expires_at_reflect_the_record(self) -> None:
        record = _record()
        session = AuthorizationSession(FakeManager(), record)

        assert session.id == "session-1"
        assert session.status is AuthorizationStatus.QUEUED
        assert session.expires_at == record.expires_at


class TestAuthorizationUrl:
    def test_blocks_until_the_url_is_set_then_returns_it(self) -> None:
        record = _record()
        session = AuthorizationSession(FakeManager(), record)

        def _set_url_soon() -> None:
            time.sleep(0.05)
            record._advance(
                AuthorizationStatus.WAITING_FOR_USER, authorization_url="https://cb/auth?state=s1"
            )

        threading.Thread(target=_set_url_soon).start()

        deadline = time.monotonic() + _WAIT_TIMEOUT
        result: list[str] = []

        def _read() -> None:
            result.append(session.authorization_url)

        reader = threading.Thread(target=_read)
        reader.start()
        reader.join(timeout=_WAIT_TIMEOUT)
        assert time.monotonic() < deadline
        assert result == ["https://cb/auth?state=s1"]

    def test_raises_terminal_exception_if_session_fails_before_a_url_is_set(self) -> None:
        record = _record()
        session = AuthorizationSession(FakeManager(), record)
        record.mark_failed(AuthorizationRejectedError("gdrive", "bad client_id"))

        with pytest.raises(AuthorizationRejectedError, match="bad client_id"):
            _ = session.authorization_url

    def test_raises_expired_error_once_the_deadline_has_passed_with_no_url(self) -> None:
        record = _record(expires_in=timedelta(seconds=-1))
        session = AuthorizationSession(FakeManager(), record)

        with pytest.raises(Exception, match="expired"):
            _ = session.authorization_url


class TestWait:
    def test_wait_returns_the_terminal_result(self) -> None:
        record = _record()
        session = AuthorizationSession(FakeManager(), record)
        result = AuthorizationResult(remote_name="gdrive", config=Config(""))
        record.mark_succeeded(result)

        assert session.wait(timeout=_WAIT_TIMEOUT) is result

    def test_wait_raises_the_terminal_exception(self) -> None:
        record = _record()
        session = AuthorizationSession(FakeManager(), record)
        record.mark_failed(AuthorizationRejectedError("gdrive", "denied"))

        with pytest.raises(AuthorizationRejectedError, match="denied"):
            session.wait(timeout=_WAIT_TIMEOUT)

    def test_wait_times_out_without_settling(self) -> None:
        record = _record()
        session = AuthorizationSession(FakeManager(), record)

        with pytest.raises(TimeoutError):
            session.wait(timeout=0.05)
        assert session.status is AuthorizationStatus.QUEUED

    def test_multiple_waiters_all_receive_the_result(self) -> None:
        record = _record()
        session = AuthorizationSession(FakeManager(), record)
        result = AuthorizationResult(remote_name="gdrive", config=Config(""))

        outcomes: list[AuthorizationResult] = []

        def _wait() -> None:
            outcomes.append(session.wait(timeout=_WAIT_TIMEOUT))

        threads = [threading.Thread(target=_wait) for _ in range(5)]
        for t in threads:
            t.start()
        time.sleep(0.05)
        record.mark_succeeded(result)
        for t in threads:
            t.join(timeout=_WAIT_TIMEOUT)

        assert outcomes == [result] * 5


class TestCancelAndClose:
    def test_cancel_delegates_to_the_manager(self) -> None:
        record = _record()
        manager = FakeManager()
        session = AuthorizationSession(manager, record)

        session.cancel()

        assert manager.cancel_calls == [record]

    def test_close_delegates_to_the_manager_and_waits(self) -> None:
        record = _record()
        manager = FakeManager()
        session = AuthorizationSession(manager, record)
        result = AuthorizationResult(remote_name="gdrive", config=Config(""))
        record.mark_succeeded(result)

        session.close()

        assert manager.close_calls == [record]

    def test_close_does_not_raise_on_timeout(self) -> None:
        record = _record()
        manager = FakeManager()
        manager.close_wait_seconds = 0.05
        session = AuthorizationSession(manager, record)

        session.close()  # never settles; must not raise

        assert manager.close_calls == [record]

    def test_close_invokes_on_dispose(self) -> None:
        record = _record()
        manager = FakeManager()
        session = AuthorizationSession(manager, record)
        record.mark_succeeded(AuthorizationResult(remote_name="gdrive", config=Config("")))
        disposed = []
        session._on_dispose = lambda: disposed.append(True)

        session.close()

        assert disposed == [True]

    def test_context_manager_closes_on_exit(self) -> None:
        record = _record()
        manager = FakeManager()
        record.mark_succeeded(AuthorizationResult(remote_name="gdrive", config=Config("")))

        with AuthorizationSession(manager, record) as session:
            assert session.status is AuthorizationStatus.SUCCEEDED

        assert manager.close_calls == [record]
