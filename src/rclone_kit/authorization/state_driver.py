"""The `config/create`/`config/update` non-interactive OAuth state walker
and its fixed question-answer policy (`docs/rclone_authorization_design.md`,
"The `config/create` non-interactive OAuth state machine").

This is the one place in `rclone_kit.authorization` that knows the
`*oauth*` state names' two possible questions - if a future backend needs a
third known question, it is added here, reviewed, and tested, not inferred
at runtime. Every other question raises
`AuthorizationUnsupportedPromptError` rather than guessing an answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rclone_kit.authorization.exceptions import (
    AuthorizationRejectedError,
    AuthorizationStartError,
    AuthorizationUnsupportedPromptError,
)
from rclone_kit.authorization.types import RemoteConflictPolicy
from rclone_kit.rc.auth import ConfigStep
from rclone_kit.rc.errors import RcCallError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from rclone_kit.authorization.types import AuthorizationRequest
    from rclone_kit.rc.auth import RcAuthClient

CONFIG_REFRESH_TOKEN = "config_refresh_token"  # noqa: S105 - a question key, not a password
CONFIG_IS_LOCAL = "config_is_local"

_CLIENT_ID_KEY = "client_id"
_CLIENT_SECRET_KEY = "client_secret"  # noqa: S105 - an rclone config key name, not a password

# The confirm question `oauthutil.SharedClientIDConfigConfirm` asks before
# falling back to rclone's own built-in client_id (several backends' Config()
# functions call it when `client_id` is left blank). Pre-answered via
# `parameters`/`choices.Get()` below rather than added to `_KNOWN_ANSWERS`:
# `backendConfigStep` (`fs/backend_config.go`) resolves any question whose
# name is present in the RC call's `parameters` *before* it ever reaches the
# RC caller, so this never surfaces as a step this driver has to see - it
# only ever needs to know the two questions in `_KNOWN_ANSWERS` below.
_SHARED_CLIENT_ID_CONFIRM_KEY = "config_shared_client_id"

_KNOWN_ANSWERS = {
    CONFIG_REFRESH_TOKEN: "true",
    CONFIG_IS_LOCAL: "true",
}


def answer_for(remote_name: str, step: ConfigStep) -> str:
    """Return the fixed policy answer for `step`'s question.

    Raises `AuthorizationUnsupportedPromptError` if `step.option_name` is
    not one of the two known questions.
    """
    assert step.option_name is not None
    answer = _KNOWN_ANSWERS.get(step.option_name)
    if answer is None:
        raise AuthorizationUnsupportedPromptError(remote_name, step.state, step.option_name)
    return answer


def is_blocking_answer(step: ConfigStep) -> bool:
    """`True` when the RC call that will carry this step's answer is the
    one that blocks inside rclone's OAuth webserver wait
    (`*oauth-islocal` answered `"true"` moves to `*oauth-do`, which calls
    `configSetup()` inline)."""
    return step.option_name == CONFIG_IS_LOCAL


def encode_parameters(request: AuthorizationRequest) -> dict[str, object]:
    """Build the `parameters` map for the initial `config/create`/
    `config/update` call: `backend_options` plus the OAuth client
    credentials, using rclone's own config keys
    (`config.ConfigClientID`/`config.ConfigClientSecret`).

    When `client_id` is left unset, also pre-answers the shared-client-id
    confirmation some backends ask for in that case - see
    `_SHARED_CLIENT_ID_CONFIRM_KEY` - the same "yes" a human driving
    `rclone config create` interactively would type, so a caller that
    doesn't supply its own provider application still gets a working
    fresh-remote flow instead of an unanswerable extra question.
    """
    parameters: dict[str, object] = dict(request.backend_options)
    if request.client_id is not None:
        parameters[_CLIENT_ID_KEY] = request.client_id
    else:
        parameters[_SHARED_CLIENT_ID_CONFIRM_KEY] = "true"
    if request.client_secret is not None:
        parameters[_CLIENT_SECRET_KEY] = request.client_secret.reveal()
    return parameters


def build_call_functions(
    auth_client: RcAuthClient,
    request: AuthorizationRequest,
    *,
    config_overlay: Mapping[str, object],
) -> tuple[Callable[[], ConfigStep], Callable[[str, str], ConfigStep]]:
    """Return `(start, continue_)` closures over `auth_client` bound to
    `request`, choosing `config/create` (fresh remote, the default) or
    `config/update` (`RemoteConflictPolicy.RECONNECT`, replacing an
    existing remote's token) once, up front.

    `parameters` is sent on *every* call, not just the first: a question
    reached partway through a continuation call (several state
    transitions can run server-side within one `BackendConfig` loop
    before returning) only sees a pre-answer if that same call's own
    `parameters` carries it - see `rc/auth.py`'s module docstring.
    """
    parameters = encode_parameters(request)

    if request.on_conflict is RemoteConflictPolicy.RECONNECT:

        def start_update() -> ConfigStep:
            return auth_client.update(
                request.remote_name, parameters, config_overlay=config_overlay
            )

        def continue_update(state: str, result: str) -> ConfigStep:
            return auth_client.update_continue(
                request.remote_name, state, result, parameters, config_overlay=config_overlay
            )

        return start_update, continue_update

    def start_create() -> ConfigStep:
        return auth_client.create(
            request.remote_name, request.backend, parameters, config_overlay=config_overlay
        )

    def continue_create(state: str, result: str) -> ConfigStep:
        return auth_client.create_continue(
            request.remote_name,
            request.backend,
            state,
            result,
            parameters,
            config_overlay=config_overlay,
        )

    return start_create, continue_create


def drive(
    remote_name: str,
    start: Callable[[], ConfigStep],
    continue_: Callable[[str, str], ConfigStep],
    *,
    on_before_blocking_call: Callable[[], None] | None = None,
) -> None:
    """Drive the non-interactive config state machine to completion,
    answering the fixed two-question policy. Returns once the remote has
    been saved (`step.done`); never returns partway through.

    `on_before_blocking_call` is invoked once, immediately before the RC
    call that will block inside rclone's OAuth webserver wait - callers
    use it to start a status-watcher thread concurrently, since that call
    otherwise gives no other way to learn the OAuth URL while it blocks.

    Raises `AuthorizationStartError` if the very first call fails,
    `AuthorizationRejectedError` if a later call fails or rclone reports a
    recoverable-in-theory `Error` this driver has no question to answer
    past, and `AuthorizationUnsupportedPromptError` for any question
    outside the fixed policy.
    """
    try:
        step = start()
    except RcCallError as error:
        raise AuthorizationStartError(remote_name, error) from error

    while not step.done:
        if step.error:
            raise AuthorizationRejectedError(remote_name, step.error)
        answer = answer_for(remote_name, step)
        if is_blocking_answer(step) and on_before_blocking_call is not None:
            on_before_blocking_call()
        try:
            step = continue_(step.state, answer)
        except RcCallError as error:
            raise AuthorizationRejectedError(remote_name, str(error)) from error
