"""Offline (no real cloud credentials) proof that `AuthorizationManager`
drives a real rclone OAuth flow end to end against the built native
library, using a local fake OAuth provider in place of a real one.

Uses the `dropbox` backend - not because this is Dropbox-specific, but
because its `Config()` (`native/rclone/backend/dropbox/dropbox.go`) is a
bare `oauthutil.ConfigOut("", &oauthutil.Options{...})` that returns
straight to the finished state ("") once OAuth completes. That is the
minimal shape this driver's fixed two-question policy supports; backends
like `drive` ask a follow-up (non-OAuth) question afterward and would need
either a pre-answered parameter or an explicit addition to the driver's
known-question table (see `docs/rclone_authorization_design.md`,
"Provider application credentials").

Every leg of a real browser round trip is played out for real over
loopback: rclone's actual `/auth` and `/` handlers (via `manager.relay()`),
a real HTTP GET to the fake provider's `/authorize` endpoint, and a real
HTTP POST from rclone itself to the fake provider's `/token` endpoint -
only the provider is fake, everything downstream of it is the genuine
vendored Go code.

Skipped automatically when no built native library exists (run
`scripts/native/build.py` first).
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import NATIVE_LIBRARY_AVAILABLE

from rclone_kit.authorization.manager import AuthorizationManager
from rclone_kit.authorization.session import AuthorizationSession
from rclone_kit.authorization.types import (
    AuthorizationRequest,
    AuthorizationStatus,
    RelayRequest,
    Secret,
)
from rclone_kit.rc.client import RcClient

if TYPE_CHECKING:
    from rclone_kit.native.runtime import RcloneRuntime

pytestmark = pytest.mark.skipif(
    not NATIVE_LIBRARY_AVAILABLE,
    reason="No built native library found; run scripts/native/build.py first.",
)

_WAIT_TIMEOUT = 30.0
_FAKE_CODE = "fake-authorization-code"
_PUBLIC_CALLBACK_URL = "http://127.0.0.1:9/oauth/rclone/callback"


class _FakeOAuthProviderHandler(BaseHTTPRequestHandler):
    """A minimal OAuth2 provider: `/authorize` redirects straight back to
    the caller's `redirect_uri` with a fixed code; `/token` exchanges any
    code for a fixed access token. Neither validates the client."""

    def log_message(self, format: str, *args: object) -> None:  # quiet the test output
        pass

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/authorize":
            self.send_response(404)
            self.end_headers()
            return
        query = parse_qs(urlsplit(self.path).query)
        redirect_uri = query["redirect_uri"][0]
        state = query["state"][0]
        self.send_response(302)
        self.send_header("Location", f"{redirect_uri}?state={state}&code={_FAKE_CODE}")
        self.end_headers()

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/token":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)  # discard the form body; the fake never validates it
        body = json.dumps(
            {"access_token": "fake-access-token", "token_type": "Bearer", "expires_in": 3600}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_oauth_provider() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOAuthProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=_WAIT_TIMEOUT)


def _delete_remote(native_runtime: RcloneRuntime, remote_name: str) -> None:
    """`native_runtime` is session-scoped (`tests/native/conftest.py`) and
    shared by every native test - a remote left behind here would pollute
    the config every other native test in the session sees, e.g. a
    `listremotes()`-returns-empty assertion elsewhere."""
    RcClient(native_runtime).call("config/delete", name=remote_name)


def _request(remote_name: str, provider_addr: str) -> AuthorizationRequest:
    return AuthorizationRequest(
        remote_name=remote_name,
        backend="dropbox",
        public_callback_url=_PUBLIC_CALLBACK_URL,
        client_id="test-client-id",
        client_secret=Secret("test-client-secret"),
        backend_options={
            "auth_url": f"http://{provider_addr}/authorize",
            "token_url": f"http://{provider_addr}/token",
        },
        # OS-assigned port: avoids any risk of colliding with rclone's
        # fixed default (127.0.0.1:53682) if tests ever run in parallel
        # worker processes.
        private_listen_addr="127.0.0.1:0",
    )


def _drive_browser_through_flow(
    manager: AuthorizationManager, session: AuthorizationSession
) -> None:
    """Play both the browser's and the provider's side of a real round
    trip, using the session's own two relay path shapes exactly as a real
    public-facing deployment would - only the HTTP client issuing the
    "browser" requests is this function instead of an actual browser.
    """
    auth_url = session.authorization_url
    entry_request = urlsplit(auth_url)

    entry_response = manager.relay(
        RelayRequest(path=entry_request.path, raw_query=entry_request.query.encode())
    )
    assert entry_response.status_code in (302, 303, 307)
    provider_url = {key.lower(): value for key, value in entry_response.headers}["location"]

    browser_response = httpx.get(provider_url, follow_redirects=False)
    assert browser_response.status_code in (302, 303, 307)
    callback_query = urlsplit(browser_response.headers["location"]).query

    callback_response = manager.relay(
        RelayRequest(path="/oauth/rclone/callback", raw_query=callback_query.encode())
    )
    assert callback_response.status_code == 200


def test_full_authorization_flow_against_a_real_native_runtime(
    native_runtime: RcloneRuntime, fake_oauth_provider: str
) -> None:
    manager = AuthorizationManager.for_runtime(native_runtime)
    remote_name = f"authz-offline-{uuid.uuid4().hex[:8]}"
    session = manager.start(_request(remote_name, fake_oauth_provider))

    try:
        _drive_browser_through_flow(manager, session)
        result = session.wait(timeout=_WAIT_TIMEOUT)
    finally:
        session.close()
        _delete_remote(native_runtime, remote_name)

    assert result.remote_name == remote_name
    section = result.config.parse().sections[remote_name]
    assert section.type() == "dropbox"
    assert "token" in section.data


def test_second_session_stays_queued_until_the_first_settles(
    native_runtime: RcloneRuntime, fake_oauth_provider: str
) -> None:
    manager = AuthorizationManager.for_runtime(native_runtime)
    first_name = f"authz-offline-{uuid.uuid4().hex[:8]}"
    second_name = f"authz-offline-{uuid.uuid4().hex[:8]}"

    first = manager.start(_request(first_name, fake_oauth_provider))
    first.authorization_url  # noqa: B018 - block until admitted and WAITING_FOR_USER
    second = manager.start(_request(second_name, fake_oauth_provider))

    try:
        # the second session must not touch rclone's shared OAuth globals
        # while the first is still active
        assert second.status is AuthorizationStatus.QUEUED

        _drive_browser_through_flow(manager, first)
        first.wait(timeout=_WAIT_TIMEOUT)

        _drive_browser_through_flow(manager, second)
        second_result = second.wait(timeout=_WAIT_TIMEOUT)
    finally:
        first.close()
        second.close()
        _delete_remote(native_runtime, first_name)
        _delete_remote(native_runtime, second_name)

    assert second_result.remote_name == second_name
