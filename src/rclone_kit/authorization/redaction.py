"""Redact attacker-influenced OAuth provider text before it reaches an
exception message or field.

`docs/rclone_authorization_design.md` ("Security requirements > Secrets and
logs") requires this: an OAuth `error_description` (and similar RFC 6749
error-response fields) is attacker-influenced input echoed back through
rclone's own error text - `golang.org/x/oauth2`'s `RetrieveError` embeds the
provider's raw token-endpoint response body (`{"error":"...",
"error_description":"..."}`) verbatim in its message, and rclone's own
config-wizard `Error` field can carry that straight through. The error
*name* (`error`/`error_code`) is safe to keep per that same section; the
description, state, and code values are not.
"""

from __future__ import annotations

import re

_REDACTED = "<redacted>"
_MAX_LENGTH = 500
_TRUNCATED_SUFFIX = "...<truncated>"

_SENSITIVE_KEYS = (
    "error_description",
    "error_uri",
    "state",
    "code",
    "access_token",
    "refresh_token",
    "client_secret",
    "id_token",
)

_JSON_VALUE_PATTERNS = [
    re.compile(rf'("{key}"\s*:\s*)"(?:[^"\\]|\\.)*"', re.IGNORECASE) for key in _SENSITIVE_KEYS
]
_QUERY_VALUE_PATTERNS = [
    re.compile(rf"(\b{key}=)[^&\s]*", re.IGNORECASE) for key in _SENSITIVE_KEYS
]


def redact_provider_text(text: str) -> str:
    """Return `text` with every `_SENSITIVE_KEYS` field's value replaced -
    matched as either a JSON string value (`"error_description":"..."`) or
    a URL-query value (`error_description=...`), whichever shape
    rclone/oauth2 happened to embed - and an overall length cap so an
    oversized payload can't be embedded wholesale even if it uses a shape
    this function doesn't recognize.
    """
    redacted = text
    for pattern in _JSON_VALUE_PATTERNS:
        redacted = pattern.sub(rf'\1"{_REDACTED}"', redacted)
    for pattern in _QUERY_VALUE_PATTERNS:
        redacted = pattern.sub(rf"\1{_REDACTED}", redacted)
    if len(redacted) > _MAX_LENGTH:
        redacted = redacted[: _MAX_LENGTH - len(_TRUNCATED_SUFFIX)] + _TRUNCATED_SUFFIX
    return redacted
