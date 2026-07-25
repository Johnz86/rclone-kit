"""Unit tests for `rclone_kit.authorization.types`: `Secret`'s redaction,
`AuthorizationRequest` validation, and `AuthorizationStatus.is_terminal`."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import pytest

from rclone_kit.authorization.types import AuthorizationRequest, AuthorizationStatus, Secret

_VALID_REMOTE_NAME = "gdrive"
_VALID_BACKEND = "drive"
_VALID_PUBLIC_CALLBACK_URL = "https://example.com/oauth/rclone/callback"


class TestSecret:
    def test_reveal_returns_the_value(self) -> None:
        assert Secret("hunter2").reveal() == "hunter2"

    def test_repr_does_not_expose_the_value(self) -> None:
        assert "hunter2" not in repr(Secret("hunter2"))

    def test_str_does_not_expose_the_value(self) -> None:
        assert "hunter2" not in str(Secret("hunter2"))

    def test_equal_values_compare_equal(self) -> None:
        assert Secret("hunter2") == Secret("hunter2")

    def test_different_values_compare_unequal(self) -> None:
        assert Secret("hunter2") != Secret("other")

    def test_is_hashable(self) -> None:
        assert hash(Secret("hunter2")) == hash(Secret("hunter2"))


@dataclass(frozen=True)
class InvalidRequestCase:
    build: Callable[[], AuthorizationRequest]
    match: str


EMPTY_REMOTE_NAME = InvalidRequestCase(
    lambda: AuthorizationRequest(
        remote_name="", backend=_VALID_BACKEND, public_callback_url=_VALID_PUBLIC_CALLBACK_URL
    ),
    "remote_name",
)
EMPTY_BACKEND = InvalidRequestCase(
    lambda: AuthorizationRequest(
        remote_name=_VALID_REMOTE_NAME, backend="", public_callback_url=_VALID_PUBLIC_CALLBACK_URL
    ),
    "backend",
)
EMPTY_PUBLIC_CALLBACK_URL = InvalidRequestCase(
    lambda: AuthorizationRequest(
        remote_name=_VALID_REMOTE_NAME, backend=_VALID_BACKEND, public_callback_url=""
    ),
    "public_callback_url",
)
ZERO_EXPIRES_IN = InvalidRequestCase(
    lambda: AuthorizationRequest(
        remote_name=_VALID_REMOTE_NAME,
        backend=_VALID_BACKEND,
        public_callback_url=_VALID_PUBLIC_CALLBACK_URL,
        expires_in=timedelta(0),
    ),
    "expires_in",
)
NEGATIVE_EXPIRES_IN = InvalidRequestCase(
    lambda: AuthorizationRequest(
        remote_name=_VALID_REMOTE_NAME,
        backend=_VALID_BACKEND,
        public_callback_url=_VALID_PUBLIC_CALLBACK_URL,
        expires_in=timedelta(seconds=-1),
    ),
    "expires_in",
)

INVALID_REQUEST_CASES = [
    EMPTY_REMOTE_NAME,
    EMPTY_BACKEND,
    EMPTY_PUBLIC_CALLBACK_URL,
    ZERO_EXPIRES_IN,
    NEGATIVE_EXPIRES_IN,
]


@pytest.mark.parametrize(
    "case",
    INVALID_REQUEST_CASES,
    ids=[
        "empty_remote_name",
        "empty_backend",
        "empty_public_callback_url",
        "zero_expires_in",
        "negative_expires_in",
    ],
)
def test_invalid_authorization_request_raises(case: InvalidRequestCase) -> None:
    with pytest.raises(ValueError, match=case.match):
        case.build()


def test_valid_authorization_request_defaults() -> None:
    request = AuthorizationRequest(
        remote_name=_VALID_REMOTE_NAME,
        backend=_VALID_BACKEND,
        public_callback_url=_VALID_PUBLIC_CALLBACK_URL,
    )

    assert request.backend_options == {}
    assert request.client_id is None
    assert request.client_secret is None
    assert request.private_listen_addr is None


def test_public_callback_url_defaults_to_none_for_local_direct_mode() -> None:
    request = AuthorizationRequest(remote_name=_VALID_REMOTE_NAME, backend=_VALID_BACKEND)

    assert request.public_callback_url is None


TERMINAL_STATUSES = [
    AuthorizationStatus.SUCCEEDED,
    AuthorizationStatus.FAILED,
    AuthorizationStatus.CANCELLED,
    AuthorizationStatus.EXPIRED,
    AuthorizationStatus.CLOSED,
]

NON_TERMINAL_STATUSES = [
    AuthorizationStatus.QUEUED,
    AuthorizationStatus.STARTING,
    AuthorizationStatus.WAITING_FOR_USER,
    AuthorizationStatus.COMPLETING,
]


@pytest.mark.parametrize("status", TERMINAL_STATUSES, ids=[s.value for s in TERMINAL_STATUSES])
def test_terminal_statuses_report_is_terminal(status: AuthorizationStatus) -> None:
    assert status.is_terminal is True


@pytest.mark.parametrize(
    "status", NON_TERMINAL_STATUSES, ids=[s.value for s in NON_TERMINAL_STATUSES]
)
def test_non_terminal_statuses_report_not_is_terminal(status: AuthorizationStatus) -> None:
    assert status.is_terminal is False
