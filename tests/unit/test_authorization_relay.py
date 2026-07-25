"""Unit tests for `rclone_kit.authorization.relay`: path routing, query
parsing, and the actual HTTP forward to a private listener.

`forward()` is exercised against a real, tiny local HTTP server rather than
a mocked `httpx` - the request/response translation (redirects passed
through unchanged, hop-by-hop headers stripped, response size capped) is
exactly the kind of thing that's easy to get subtly wrong when mocked and
cheap to verify for real over loopback.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rclone_kit.authorization import relay
from rclone_kit.authorization.exceptions import AuthorizationRelayError
from rclone_kit.authorization.types import RelayRequest

_LARGE_BODY_BYTES = b"x" * 100


class _FakeAuthServer(BaseHTTPRequestHandler):
    """Mimics rclone's private OAuth listener closely enough for
    `forward()`'s translation logic: `/auth` redirects (like rclone's own
    `/auth` handler does to the provider), anything else replies 200 with
    a body reflecting the received query string."""

    def log_message(self, format: str, *args: object) -> None:  # quiet the test output
        pass

    def do_GET(self) -> None:
        if self.path.startswith("/auth"):
            self.send_response(307)
            self.send_header("Location", "https://provider.example.com/authorize?state=xyz")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("X-Custom", "keep-me")
        body = _LARGE_BODY_BYTES if "big=1" in self.path else self.path.encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_auth_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAuthServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5.0)


class TestIsAuthPath:
    def test_bare_auth_path(self) -> None:
        assert relay.is_auth_path("/auth") is True

    def test_public_prefixed_auth_path(self) -> None:
        assert relay.is_auth_path("/oauth/rclone/callback/auth") is True

    def test_trailing_slash_is_tolerated(self) -> None:
        assert relay.is_auth_path("/oauth/rclone/callback/auth/") is True

    def test_callback_root_path_is_not_auth(self) -> None:
        assert relay.is_auth_path("/oauth/rclone/callback") is False

    def test_root_path_is_not_auth(self) -> None:
        assert relay.is_auth_path("/") is False


class TestExtractState:
    def test_state_present(self) -> None:
        assert relay.extract_state(b"state=abc123&code=xyz") == "abc123"

    def test_state_absent(self) -> None:
        assert relay.extract_state(b"code=xyz") is None

    def test_empty_query(self) -> None:
        assert relay.extract_state(b"") is None


class TestForward:
    def test_disallowed_method_raises_without_a_request(self, fake_auth_server: str) -> None:
        with pytest.raises(AuthorizationRelayError, match="POST"):
            relay.forward(
                fake_auth_server, RelayRequest(path="/auth", raw_query=b"", method="POST")
            )

    def test_auth_path_redirect_is_passed_through_unchanged(self, fake_auth_server: str) -> None:
        response = relay.forward(
            fake_auth_server,
            RelayRequest(path="/oauth/rclone/callback/auth", raw_query=b"state=s1"),
        )

        assert response.status_code == 307
        headers = {key.lower(): value for key, value in response.headers}
        assert headers["location"] == "https://provider.example.com/authorize?state=xyz"

    def test_hop_by_hop_headers_are_stripped(self, fake_auth_server: str) -> None:
        response = relay.forward(
            fake_auth_server, RelayRequest(path="/oauth/rclone/callback/auth", raw_query=b"")
        )

        header_names = {key.lower() for key, _ in response.headers}
        assert "connection" not in header_names

    def test_callback_path_forwards_to_private_root(self, fake_auth_server: str) -> None:
        response = relay.forward(
            fake_auth_server,
            RelayRequest(path="/oauth/rclone/callback", raw_query=b"state=s1&code=abc"),
        )

        assert response.status_code == 200
        assert response.body == b"/?state=s1&code=abc"
        header_names = {key.lower() for key, _ in response.headers}
        assert "x-custom" in header_names

    def test_raw_query_is_preserved_verbatim(self, fake_auth_server: str) -> None:
        response = relay.forward(
            fake_auth_server,
            RelayRequest(path="/oauth/rclone/callback/auth", raw_query=b"state=s1"),
        )

        # the fake server's /auth handler ignores the query, but a
        # malformed/undecodable query must not raise before the request
        # is even sent
        assert response.status_code == 307

    def test_response_body_is_capped(
        self, fake_auth_server: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(relay, "_MAX_RESPONSE_BYTES", 10)

        response = relay.forward(
            fake_auth_server, RelayRequest(path="/oauth/rclone/callback", raw_query=b"big=1")
        )

        assert response.body == _LARGE_BODY_BYTES[:10]

    def test_connection_failure_raises_relay_error(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            unused_port = probe.getsockname()[1]

        with pytest.raises(AuthorizationRelayError, match="OAuth listener"):
            relay.forward(f"http://127.0.0.1:{unused_port}", RelayRequest(path="/", raw_query=b""))
