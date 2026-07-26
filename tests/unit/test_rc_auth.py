"""Unit tests for `rclone_kit.rc.auth`'s RC config/authorization boundary
(`RcloneRcAuthClient`): request mapping and strict response parsing.

Uses a fake `RcCallable` driven by canned per-method responses (or
exceptions), mirroring `test_rc_jobs.py`'s style. Wire shapes asserted here
were read directly from the vendored Go source (see `rc/auth.py`'s module
docstring), not merely assumed.
"""

import pytest

from rclone_kit.rc.auth import ConfigStep, OAuthStatus, RcloneRcAuthClient
from rclone_kit.rc.errors import RcCallError


class FakeRcClient:
    """A fake `RcCallable` returning one queued response (or raising one
    queued exception) per call to a given method, repeating the last
    queued entry once exhausted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._queues: dict[str, list[object]] = {}

    def queue(self, method: str, *responses: object) -> None:
        self._queues[method] = list(responses)

    def call(self, method: str, **params: object) -> dict:
        self.calls.append((method, params))
        queue = self._queues.get(method)
        if not queue:
            raise AssertionError(f"no queued response for {method!r}")
        response = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, dict)
        return response


def _config_out(*, state: str = "", option_name: str | None = None, error: str = "") -> dict:
    option = None if option_name is None else {"Name": option_name}
    return {"State": state, "Option": option, "Error": error, "Result": ""}


class TestCreate:
    def test_create_sends_type_parameters_and_non_interactive_opt(self) -> None:
        client = FakeRcClient()
        client.queue(
            "config/create", _config_out(state="*oauth,drive", option_name="config_is_local")
        )
        auth_client = RcloneRcAuthClient(client)

        step = auth_client.create("gdrive", "drive", {"scope": "drive"})

        assert step == ConfigStep(state="*oauth,drive", option_name="config_is_local", error=None)
        assert client.calls == [
            (
                "config/create",
                {
                    "name": "gdrive",
                    "parameters": {"scope": "drive"},
                    "opt": {"nonInteractive": True},
                    "type": "drive",
                },
            )
        ]

    def test_create_with_config_overlay_sends_underscore_config(self) -> None:
        client = FakeRcClient()
        client.queue("config/create", _config_out())
        auth_client = RcloneRcAuthClient(client)

        auth_client.create(
            "gdrive", "drive", {}, config_overlay={"OAuthRedirectURL": "https://example.com/cb"}
        )

        _method, params = client.calls[0]
        assert params["_config"] == {"OAuthRedirectURL": "https://example.com/cb"}

    def test_create_terminal_step_is_done(self) -> None:
        client = FakeRcClient()
        client.queue("config/create", _config_out(state=""))
        auth_client = RcloneRcAuthClient(client)

        step = auth_client.create("gdrive", "drive", {})

        assert step.done is True
        assert step.needs_answer is False


class TestCreateContinue:
    def test_continue_sends_state_result_and_continue_flag(self) -> None:
        client = FakeRcClient()
        client.queue("config/create", _config_out(state=""))
        auth_client = RcloneRcAuthClient(client)

        step = auth_client.create_continue("gdrive", "drive", "*oauth,drive", "true", {})

        assert step.done is True
        assert client.calls == [
            (
                "config/create",
                {
                    "name": "gdrive",
                    "parameters": {},
                    "opt": {
                        "nonInteractive": True,
                        "continue": True,
                        "state": "*oauth,drive",
                        "result": "true",
                    },
                    "type": "drive",
                },
            )
        ]

    def test_continue_forwards_parameters(self) -> None:
        # A pre-answer for a question reached partway through a
        # continuation call only works if *that* call's own parameters
        # carry it - see rc/auth.py's module docstring.
        client = FakeRcClient()
        client.queue("config/create", _config_out(state=""))
        auth_client = RcloneRcAuthClient(client)

        auth_client.create_continue(
            "gdrive", "drive", "*oauth,drive", "true", {"config_change_team_drive": "false"}
        )

        _method, params = client.calls[0]
        assert params["parameters"] == {"config_change_team_drive": "false"}


class TestUpdate:
    def test_update_omits_type_parameter(self) -> None:
        client = FakeRcClient()
        client.queue("config/update", _config_out(option_name="config_refresh_token"))
        auth_client = RcloneRcAuthClient(client)

        step = auth_client.update("gdrive", {})

        assert step.option_name == "config_refresh_token"
        _method, params = client.calls[0]
        assert "type" not in params

    def test_update_continue_omits_type_parameter(self) -> None:
        client = FakeRcClient()
        client.queue("config/update", _config_out(state=""))
        auth_client = RcloneRcAuthClient(client)

        auth_client.update_continue("gdrive", "*oauth-confirm,drive", "true", {})

        method, params = client.calls[0]
        assert method == "config/update"
        assert "type" not in params


class TestConfigStepParsing:
    def test_question_step_carries_option_name(self) -> None:
        client = FakeRcClient()
        client.queue("config/create", _config_out(state="s1", option_name="config_is_local"))
        auth_client = RcloneRcAuthClient(client)

        step = auth_client.create("gdrive", "drive", {})

        assert step.needs_answer is True
        assert step.option_name == "config_is_local"
        assert step.error is None

    def test_empty_error_string_becomes_none(self) -> None:
        client = FakeRcClient()
        client.queue(
            "config/create", _config_out(state="s1", option_name="config_is_local", error="")
        )
        auth_client = RcloneRcAuthClient(client)

        assert auth_client.create("gdrive", "drive", {}).error is None

    def test_non_empty_error_is_preserved(self) -> None:
        client = FakeRcClient()
        client.queue(
            "config/create", _config_out(state="s1", option_name="config_is_local", error="boom")
        )
        auth_client = RcloneRcAuthClient(client)

        assert auth_client.create("gdrive", "drive", {}).error == "boom"

    def test_option_without_name_is_rejected(self) -> None:
        client = FakeRcClient()
        client.queue("config/create", {"State": "s1", "Option": {}, "Error": "", "Result": ""})
        auth_client = RcloneRcAuthClient(client)

        with pytest.raises(ValueError, match=r"Option\.Name"):
            auth_client.create("gdrive", "drive", {})

    def test_missing_state_key_defaults_to_finished(self) -> None:
        client = FakeRcClient()
        client.queue("config/create", {})
        auth_client = RcloneRcAuthClient(client)

        assert auth_client.create("gdrive", "drive", {}).done is True


class TestDelete:
    def test_delete_sends_name(self) -> None:
        client = FakeRcClient()
        client.queue("config/delete", {})
        auth_client = RcloneRcAuthClient(client)

        auth_client.delete("gdrive")

        assert client.calls == [("config/delete", {"name": "gdrive"})]


class TestListRemotes:
    def test_parses_remotes_list(self) -> None:
        client = FakeRcClient()
        client.queue("config/listremotes", {"remotes": ["gdrive", "s3"]})
        auth_client = RcloneRcAuthClient(client)

        assert auth_client.listremotes() == ("gdrive", "s3")

    def test_non_list_remotes_is_rejected(self) -> None:
        client = FakeRcClient()
        client.queue("config/listremotes", {"remotes": "not a list"})
        auth_client = RcloneRcAuthClient(client)

        with pytest.raises(ValueError, match="remotes"):
            auth_client.listremotes()


class TestOAuthStatus:
    def test_running_status_carries_auth_url(self) -> None:
        client = FakeRcClient()
        client.queue(
            "config/oauthstatus",
            {"status": "running", "authUrl": "http://127.0.0.1:53682/auth?state=xyz"},
        )
        auth_client = RcloneRcAuthClient(client)

        status = auth_client.oauth_status()

        assert status == OAuthStatus(running=True, auth_url="http://127.0.0.1:53682/auth?state=xyz")

    def test_stopped_status_has_no_auth_url(self) -> None:
        client = FakeRcClient()
        client.queue("config/oauthstatus", {"status": "stopped"})
        auth_client = RcloneRcAuthClient(client)

        assert auth_client.oauth_status() == OAuthStatus(running=False, auth_url=None)

    def test_invalid_status_value_is_rejected(self) -> None:
        client = FakeRcClient()
        client.queue("config/oauthstatus", {"status": "unknown"})
        auth_client = RcloneRcAuthClient(client)

        with pytest.raises(ValueError, match="status"):
            auth_client.oauth_status()


class TestOAuthStop:
    def test_stop_sends_no_parameters(self) -> None:
        client = FakeRcClient()
        client.queue("config/oauthstop", {})
        auth_client = RcloneRcAuthClient(client)

        auth_client.oauth_stop()

        assert client.calls == [("config/oauthstop", {})]

    def test_stop_when_nothing_running_is_idempotent(self) -> None:
        client = FakeRcClient()
        client.queue(
            "config/oauthstop",
            RcCallError(
                "config/oauthstop", 500, {"error": "no oauth authentication is in progress"}
            ),
        )
        auth_client = RcloneRcAuthClient(client)

        auth_client.oauth_stop()  # must not raise

    def test_stop_other_failures_propagate(self) -> None:
        client = FakeRcClient()
        other_error = RcCallError("config/oauthstop", 500, {"error": "internal error"})
        client.queue("config/oauthstop", other_error)
        auth_client = RcloneRcAuthClient(client)

        with pytest.raises(RcCallError):
            auth_client.oauth_stop()
