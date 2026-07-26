"""Unit tests for `rclone_kit.authorization.redaction.redact_provider_text`
and its use in `AuthorizationRejectedError`/`AuthorizationStartError`/
`AuthorizationRelayError`.

Regression coverage for a real gap found in review: these exceptions used
to interpolate rclone's raw OAuth error text verbatim, even though it can
carry an attacker-influenced `error_description` straight through from the
provider (`golang.org/x/oauth2`'s `RetrieveError` embeds the token
endpoint's raw response body). `docs/rclone_authorization_design.md`
("Security requirements > Secrets and logs") requires this text be
redacted before it reaches any exception.
"""

from rclone_kit.authorization.exceptions import (
    AuthorizationRejectedError,
    AuthorizationRelayError,
    AuthorizationStartError,
)
from rclone_kit.authorization.redaction import redact_provider_text


def test_redacts_json_style_error_description() -> None:
    text = 'oauth2: cannot fetch token: 400 Bad Request\nResponse: {"error":"invalid_grant","error_description":"attacker controlled text"}'

    redacted = redact_provider_text(text)

    assert "attacker controlled text" not in redacted
    assert '"error":"invalid_grant"' in redacted  # the error *name* is safe to keep
    assert '"error_description":"<redacted>"' in redacted


def test_redacts_query_style_code_and_state() -> None:
    text = "callback failed for ?code=4/0AX4XfWi_secret&state=abc123&scope=drive"

    redacted = redact_provider_text(text)

    assert "4/0AX4XfWi_secret" not in redacted
    assert "abc123" not in redacted
    assert "code=<redacted>" in redacted
    assert "state=<redacted>" in redacted
    assert "scope=drive" in redacted  # not on the sensitive list


def test_truncates_an_oversized_payload_even_if_unrecognized() -> None:
    text = "x" * 10_000

    redacted = redact_provider_text(text)

    assert len(redacted) < len(text)
    assert redacted.endswith("<truncated>")


def test_plain_text_without_sensitive_fields_passes_through_unchanged() -> None:
    text = "oauth authentication was cancelled"

    assert redact_provider_text(text) == text


def test_authorization_rejected_error_reason_field_is_redacted() -> None:
    raw = '{"error":"access_denied","error_description":"leaked secret text"}'

    error = AuthorizationRejectedError("gdrive", raw)

    assert "leaked secret text" not in error.reason
    assert "leaked secret text" not in str(error)
    assert "access_denied" in error.reason


def test_authorization_start_error_message_is_redacted() -> None:
    cause = RuntimeError('{"error":"server_error","error_description":"leaked secret text"}')

    error = AuthorizationStartError("gdrive", cause)

    assert "leaked secret text" not in str(error)
    # The original cause object is still preserved for internal use/__cause__.
    assert error.cause is cause


def test_authorization_relay_error_detail_is_redacted() -> None:
    error = AuthorizationRelayError("failed to reach ?code=super-secret-code&state=xyz")

    assert "super-secret-code" not in str(error)
    assert "xyz" not in str(error)
