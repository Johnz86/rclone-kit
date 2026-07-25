"""Unit tests for `rclone_kit.authorization.state_driver`: the
`config/create`/`config/update` non-interactive state walker and its fixed
two-question answer policy."""

import pytest

from rclone_kit.authorization.exceptions import (
    AuthorizationRejectedError,
    AuthorizationStartError,
    AuthorizationUnsupportedPromptError,
)
from rclone_kit.authorization.state_driver import (
    CONFIG_IS_LOCAL,
    CONFIG_REFRESH_TOKEN,
    answer_for,
    build_call_functions,
    drive,
    encode_parameters,
    is_blocking_answer,
)
from rclone_kit.authorization.types import AuthorizationRequest, RemoteConflictPolicy, Secret
from rclone_kit.rc.auth import ConfigStep
from rclone_kit.rc.errors import RcCallError


class FakeAuthClient:
    """A fake `RcAuthClient` scripted with queued `ConfigStep`/exception
    responses for `create`/`create_continue`/`update`/`update_continue`,
    mirroring `test_rc_jobs.py`'s `FakeRcClient`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._queues: dict[str, list[object]] = {}

    def queue(self, method: str, *responses: object) -> None:
        self._queues[method] = list(responses)

    def _next(self, method: str, args: tuple) -> ConfigStep:
        self.calls.append((method, args))
        queue = self._queues.get(method)
        if not queue:
            raise AssertionError(f"no queued response for {method!r}")
        response = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, ConfigStep)
        return response

    def create(self, name, backend, parameters, *, config_overlay=None):
        return self._next("create", (name, backend, dict(parameters), dict(config_overlay or {})))

    def create_continue(self, name, backend, state, result, parameters, *, config_overlay=None):  # noqa: ARG002
        return self._next("create_continue", (name, backend, state, result, dict(parameters)))

    def update(self, name, parameters, *, config_overlay=None):  # noqa: ARG002
        return self._next("update", (name, dict(parameters)))

    def update_continue(self, name, state, result, parameters, *, config_overlay=None):  # noqa: ARG002
        return self._next("update_continue", (name, state, result, dict(parameters)))

    def delete(self, name):  # noqa: ARG002
        raise AssertionError("delete() should not be called by the state driver")

    def listremotes(self):
        raise AssertionError("listremotes() should not be called by the state driver")

    def oauth_status(self):
        raise AssertionError("oauth_status() should not be called by the state driver")

    def oauth_stop(self):
        raise AssertionError("oauth_stop() should not be called by the state driver")


_REQUEST = AuthorizationRequest(
    remote_name="gdrive",
    backend="drive",
    public_callback_url="https://example.com/oauth/rclone/callback",
    backend_options={"scope": "drive"},
    client_id="cid",
    client_secret=Secret("csecret"),
)

_RECONNECT_REQUEST = AuthorizationRequest(
    remote_name="gdrive",
    backend="drive",
    public_callback_url="https://example.com/oauth/rclone/callback",
    on_conflict=RemoteConflictPolicy.RECONNECT,
)

_DONE_STEP = ConfigStep(state="", option_name=None, error=None)
_IS_LOCAL_STEP = ConfigStep(state="s1", option_name=CONFIG_IS_LOCAL, error=None)
_REFRESH_TOKEN_STEP = ConfigStep(state="s0", option_name=CONFIG_REFRESH_TOKEN, error=None)
_UNKNOWN_QUESTION_STEP = ConfigStep(state="s2", option_name="config_something_else", error=None)
_ERROR_STEP = ConfigStep(state="s3", option_name=None, error="boom")


class TestAnswerFor:
    def test_config_is_local_answers_true(self) -> None:
        assert answer_for("gdrive", _IS_LOCAL_STEP) == "true"

    def test_config_refresh_token_answers_true(self) -> None:
        assert answer_for("gdrive", _REFRESH_TOKEN_STEP) == "true"

    def test_unknown_question_raises(self) -> None:
        with pytest.raises(AuthorizationUnsupportedPromptError) as excinfo:
            answer_for("gdrive", _UNKNOWN_QUESTION_STEP)
        assert excinfo.value.option_name == "config_something_else"
        assert excinfo.value.state == "s2"


class TestIsBlockingAnswer:
    def test_config_is_local_blocks(self) -> None:
        assert is_blocking_answer(_IS_LOCAL_STEP) is True

    def test_config_refresh_token_does_not_block(self) -> None:
        assert is_blocking_answer(_REFRESH_TOKEN_STEP) is False


class TestEncodeParameters:
    def test_merges_backend_options_and_credentials(self) -> None:
        parameters = encode_parameters(_REQUEST)

        assert parameters == {"scope": "drive", "client_id": "cid", "client_secret": "csecret"}

    def test_no_client_id_pre_answers_the_shared_client_id_confirmation(self) -> None:
        request = AuthorizationRequest(
            remote_name="gdrive",
            backend="drive",
            public_callback_url="https://example.com/cb",
        )

        assert encode_parameters(request) == {"config_shared_client_id": "true"}

    def test_explicit_client_id_does_not_pre_answer_the_shared_client_id_confirmation(
        self,
    ) -> None:
        request = AuthorizationRequest(
            remote_name="gdrive",
            backend="drive",
            public_callback_url="https://example.com/cb",
            client_id="cid",
        )

        assert encode_parameters(request) == {"client_id": "cid"}


class TestDriveCreate:
    def test_instant_success_never_calls_continue(self) -> None:
        client = FakeAuthClient()
        client.queue("create", _DONE_STEP)
        start, continue_ = build_call_functions(client, _REQUEST, config_overlay={})

        drive("gdrive", start, continue_)  # must not raise

        assert [call[0] for call in client.calls] == ["create"]

    def test_full_fresh_remote_flow_answers_is_local_and_completes(self) -> None:
        client = FakeAuthClient()
        client.queue("create", _IS_LOCAL_STEP)
        client.queue("create_continue", _DONE_STEP)
        start, continue_ = build_call_functions(client, _REQUEST, config_overlay={})
        blocking_calls: list[bool] = []

        drive(
            "gdrive",
            start,
            continue_,
            on_before_blocking_call=lambda: blocking_calls.append(True),
        )

        assert [call[0] for call in client.calls] == ["create", "create_continue"]
        assert client.calls[1][1] == (
            "gdrive",
            "drive",
            "s1",
            "true",
            {"scope": "drive", "client_id": "cid", "client_secret": "csecret"},
        )
        assert blocking_calls == [True]

    def test_reconnect_flow_answers_refresh_token_then_is_local(self) -> None:
        client = FakeAuthClient()
        client.queue("update", _REFRESH_TOKEN_STEP)
        client.queue("update_continue", _IS_LOCAL_STEP, _DONE_STEP)
        start, continue_ = build_call_functions(client, _RECONNECT_REQUEST, config_overlay={})
        blocking_calls: list[bool] = []

        drive(
            "gdrive",
            start,
            continue_,
            on_before_blocking_call=lambda: blocking_calls.append(True),
        )

        assert [call[0] for call in client.calls] == [
            "update",
            "update_continue",
            "update_continue",
        ]
        assert blocking_calls == [True]

    def test_unrecognized_question_raises_without_answering(self) -> None:
        client = FakeAuthClient()
        client.queue("create", _UNKNOWN_QUESTION_STEP)
        start, continue_ = build_call_functions(client, _REQUEST, config_overlay={})

        with pytest.raises(AuthorizationUnsupportedPromptError):
            drive("gdrive", start, continue_)

        assert [call[0] for call in client.calls] == ["create"]

    def test_recoverable_error_step_raises_rejected(self) -> None:
        client = FakeAuthClient()
        client.queue("create", _IS_LOCAL_STEP)
        client.queue("create_continue", _ERROR_STEP)
        start, continue_ = build_call_functions(client, _REQUEST, config_overlay={})

        with pytest.raises(AuthorizationRejectedError, match="boom"):
            drive("gdrive", start, continue_)

    def test_first_call_failure_raises_start_error(self) -> None:
        client = FakeAuthClient()
        cause = RcCallError("config/create", 500, {"error": "bad client_id"})
        client.queue("create", cause)
        start, continue_ = build_call_functions(client, _REQUEST, config_overlay={})

        with pytest.raises(AuthorizationStartError) as excinfo:
            drive("gdrive", start, continue_)
        assert excinfo.value.__cause__ is cause

    def test_continuation_failure_raises_rejected_not_start_error(self) -> None:
        client = FakeAuthClient()
        client.queue("create", _IS_LOCAL_STEP)
        client.queue(
            "create_continue",
            RcCallError("config/create", 500, {"error": "oauth authentication was cancelled"}),
        )
        start, continue_ = build_call_functions(client, _REQUEST, config_overlay={})

        with pytest.raises(AuthorizationRejectedError, match="oauth authentication was cancelled"):
            drive("gdrive", start, continue_)


class TestBuildCallFunctionsRouting:
    def test_reject_policy_routes_to_create(self) -> None:
        client = FakeAuthClient()
        client.queue("create", _DONE_STEP)
        start, _continue = build_call_functions(client, _REQUEST, config_overlay={})

        start()

        assert client.calls[0][0] == "create"

    def test_reconnect_policy_routes_to_update(self) -> None:
        client = FakeAuthClient()
        client.queue("update", _DONE_STEP)
        start, _continue = build_call_functions(client, _RECONNECT_REQUEST, config_overlay={})

        start()

        assert client.calls[0][0] == "update"
