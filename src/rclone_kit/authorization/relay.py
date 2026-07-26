"""Public-callback <-> rclone-private-listener request translation and
forwarding (`docs/rclone_authorization_design.md`, "Relay design").

Owns the public/private request translation and the actual HTTP forward;
nothing about session routing or the state machine -
`AuthorizationManager.relay()` (a different module) looks up the one
currently-active session and validates `state` before calling `forward()`
here, and is the only caller that decides *which* private listener address
to forward to. This module never selects that address from anything in
the request itself, which is what prevents it from becoming an SSRF
primitive.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx

from rclone_kit.authorization.exceptions import AuthorizationRelayError
from rclone_kit.authorization.types import RelayRequest, RelayResponse

_AUTH_PATH_SUFFIX = "/auth"
_ALLOWED_METHODS = frozenset({"GET"})
_REQUEST_TIMEOUT_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 1_000_000  # generous cap for rclone's small auth page

# Headers that must not be copied verbatim from a proxied response - either
# meaningful only for this specific hop (RFC 9110 7.6.1) or recomputed by
# whatever serves the final response (`Content-Length`, since `body` may be
# truncated by `_MAX_RESPONSE_BYTES`).
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
)


def is_auth_path(path: str) -> bool:
    """`True` for the entry URL shown to the user (rclone's `/auth`
    handler, which validates `state` and redirects the browser to the
    provider); `False` for every other path, which is the provider's
    callback and belongs to rclone's `/` handler."""
    return path.rstrip("/").endswith(_AUTH_PATH_SUFFIX)


def extract_state(raw_query: bytes) -> str | None:
    """Extract the `state` query parameter, or `None` if absent."""
    values = parse_qs(raw_query.decode("utf-8", errors="replace")).get("state")
    return values[0] if values else None


def _private_path(path: str) -> str:
    return "/auth" if is_auth_path(path) else "/"


def forward(private_base_url: str, request: RelayRequest) -> RelayResponse:
    """Forward `request` to rclone's private OAuth listener at
    `private_base_url` and return its response, translated into a
    framework-neutral `RelayResponse`.

    Preserves the raw query string as-is (duplicate/escaped parameters
    survive intact), does not follow redirects (the provider `Location`
    redirect from rclone's `/auth` handler must reach the browser
    unchanged, not be consumed here), and caps the response body to
    `_MAX_RESPONSE_BYTES`.
    """
    if request.method not in _ALLOWED_METHODS:
        raise AuthorizationRelayError(f"method {request.method!r} is not allowed")

    url = f"{private_base_url}{_private_path(request.path)}"
    query = request.raw_query.decode("utf-8", errors="replace")
    if query:
        url = f"{url}?{query}"

    try:
        response = httpx.get(url, timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=False)
    except httpx.HTTPError as error:
        raise AuthorizationRelayError(
            f"failed to reach rclone's OAuth listener: {error}", error
        ) from error

    body = response.content[:_MAX_RESPONSE_BYTES]
    headers = tuple(
        (key, value)
        for key, value in response.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    )
    return RelayResponse(status_code=response.status_code, headers=headers, body=body)
