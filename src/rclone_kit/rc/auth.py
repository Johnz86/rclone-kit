"""RC config/authorization boundary: typed adapters over one `RcCallable`
for the non-interactive `config/create`/`config/update` OAuth state
machine, plus `config/delete`, `config/listremotes`, `config/oauthstatus`,
and `config/oauthstop`. Mirrors `rc/jobs.py`'s `RcJobClient` shape.

Wire shapes below were read directly from the vendored Go source
(`native/rclone/fs/config/rc.go`, `native/rclone/fs/backend_config.go`,
`native/rclone/lib/oauthutil/oauthutil.go`), not merely assumed:

- `config/create`/`config/update` with `opt.nonInteractive=true` return
  `fs.ConfigOut` reshaped through JSON (`rc.Reshape` round-trips the Go
  struct through `encoding/json`; none of `ConfigOut`'s JSON-visible
  fields use `omitempty`), always exactly `{"State": str, "Option":
  {"Name": str, ...} | null, "Error": str, "Result": str}`. Per
  `BackendConfig`'s loop, checked in this priority order: `State == ""`
  means finished (the remote is saved); else a non-null `Option` means a
  question (`Option.Name` identifies which one); else a non-empty `Error`
  means an error the caller must stop on, since it isn't one of the two
  questions this driver knows how to answer past.
- `config/listremotes` returns `{"remotes": [str, ...]}`.
- `config/oauthstatus` returns `{"status": "running"|"stopped", "authUrl":
  str}` - `authUrl` is present only while `status == "running"`.
- `config/oauthstop` returns `{}` on success, or fails (`RcCallError`)
  with `payload["error"] == "no oauth authentication is in progress"` if
  no flow is currently active - treated as already-idempotently-stopped,
  matching `RcloneRcJobClient.stop()`'s "not found" handling.
- `config/create` requires a `type` parameter on every call (including
  continuations), even though `CreateRemote` only reads it on the first,
  non-`continue` call; `config/update` never takes one.
- `parameters` (`choices` server-side, `fs/config/config.go`'s
  `updateRemote`) is scoped to one RC call, not the whole flow: a
  pre-answer for a question reached partway through a *continuation*
  call (state transitions inside one `BackendConfig` loop run entirely
  server-side and can reach several questions before returning) is only
  honored if that same continuation call's own `parameters` carries it -
  supplying it only on the first call does nothing for a question the
  first call never reaches itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from rclone_kit.rc.errors import RcCallError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rclone_kit.rc.client import RcCallable

_NO_OAUTH_IN_PROGRESS_MESSAGE = "no oauth authentication is in progress"


@dataclass(frozen=True)
class ConfigStep:
    """One step of `config/create`/`config/update`'s non-interactive state
    machine, as returned to the RC caller by `BackendConfig`'s loop."""

    state: str
    option_name: str | None
    error: str | None

    @property
    def done(self) -> bool:
        """`True` once the state machine has finished and the remote has
        been saved: `state == ""`, per `BackendConfig`'s own finished
        check, checked before `option_name`/`error`."""
        return self.state == ""

    @property
    def needs_answer(self) -> bool:
        return self.option_name is not None


@dataclass(frozen=True)
class OAuthStatus:
    """`config/oauthstatus`'s parsed response."""

    running: bool
    auth_url: str | None


class RcAuthClient(Protocol):
    """Narrow config/authorization interface the authorization state
    driver/session depend on, so their tests can supply a fake without a
    real `RcClient`."""

    def create(
        self,
        name: str,
        backend: str,
        parameters: Mapping[str, object],
        *,
        config_overlay: Mapping[str, object] | None = None,
    ) -> ConfigStep: ...

    def create_continue(
        self,
        name: str,
        backend: str,
        state: str,
        result: str,
        parameters: Mapping[str, object],
        *,
        config_overlay: Mapping[str, object] | None = None,
    ) -> ConfigStep: ...

    def update(
        self,
        name: str,
        parameters: Mapping[str, object],
        *,
        config_overlay: Mapping[str, object] | None = None,
    ) -> ConfigStep: ...

    def update_continue(
        self,
        name: str,
        state: str,
        result: str,
        parameters: Mapping[str, object],
        *,
        config_overlay: Mapping[str, object] | None = None,
    ) -> ConfigStep: ...

    def delete(self, name: str) -> None: ...
    def listremotes(self) -> tuple[str, ...]: ...
    def oauth_status(self) -> OAuthStatus: ...
    def oauth_stop(self) -> None: ...


def _parse_config_step(payload: Mapping[str, object]) -> ConfigStep:
    state = payload.get("State") or ""
    if not isinstance(state, str):
        raise ValueError(f"State must be a string, got {state!r}")

    raw_option = payload.get("Option")
    option_name: str | None = None
    if raw_option is not None:
        if not isinstance(raw_option, dict):
            raise ValueError(f"Option must be an object or null, got {raw_option!r}")
        name = raw_option.get("Name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"Option.Name must be a non-empty string, got {name!r}")
        option_name = name

    raw_error = payload.get("Error") or None
    if raw_error is not None and not isinstance(raw_error, str):
        raise ValueError(f"Error must be a string, got {raw_error!r}")

    return ConfigStep(state=state, option_name=option_name, error=raw_error)


def _parse_oauth_status(payload: Mapping[str, object]) -> OAuthStatus:
    status = payload.get("status")
    if status not in ("running", "stopped"):
        raise ValueError(f"status must be 'running' or 'stopped', got {status!r}")
    auth_url = payload.get("authUrl")
    if auth_url is not None and not isinstance(auth_url, str):
        raise ValueError(f"authUrl must be a string, got {auth_url!r}")
    return OAuthStatus(running=status == "running", auth_url=auth_url)


class RcloneRcAuthClient:
    """The real `RcAuthClient`, backed by one `RcCallable`."""

    def __init__(self, rc_client: RcCallable) -> None:
        self._rc_client = rc_client

    def _config_call(
        self,
        method: str,
        name: str,
        parameters: Mapping[str, object],
        opt: Mapping[str, object],
        config_overlay: Mapping[str, object] | None,
        *,
        backend: str | None = None,
    ) -> ConfigStep:
        call_params: dict[str, object] = {
            "name": name,
            "parameters": dict(parameters),
            "opt": dict(opt),
        }
        if backend is not None:
            call_params["type"] = backend
        if config_overlay:
            call_params["_config"] = dict(config_overlay)
        response = self._rc_client.call(method, **call_params)
        return _parse_config_step(response)

    def create(
        self,
        name: str,
        backend: str,
        parameters: Mapping[str, object],
        *,
        config_overlay: Mapping[str, object] | None = None,
    ) -> ConfigStep:
        return self._config_call(
            "config/create",
            name,
            parameters,
            {"nonInteractive": True},
            config_overlay,
            backend=backend,
        )

    def create_continue(
        self,
        name: str,
        backend: str,
        state: str,
        result: str,
        parameters: Mapping[str, object],
        *,
        config_overlay: Mapping[str, object] | None = None,
    ) -> ConfigStep:
        return self._config_call(
            "config/create",
            name,
            parameters,
            {"nonInteractive": True, "continue": True, "state": state, "result": result},
            config_overlay,
            backend=backend,
        )

    def update(
        self,
        name: str,
        parameters: Mapping[str, object],
        *,
        config_overlay: Mapping[str, object] | None = None,
    ) -> ConfigStep:
        return self._config_call(
            "config/update", name, parameters, {"nonInteractive": True}, config_overlay
        )

    def update_continue(
        self,
        name: str,
        state: str,
        result: str,
        parameters: Mapping[str, object],
        *,
        config_overlay: Mapping[str, object] | None = None,
    ) -> ConfigStep:
        return self._config_call(
            "config/update",
            name,
            parameters,
            {"nonInteractive": True, "continue": True, "state": state, "result": result},
            config_overlay,
        )

    def delete(self, name: str) -> None:
        self._rc_client.call("config/delete", name=name)

    def listremotes(self) -> tuple[str, ...]:
        response = self._rc_client.call("config/listremotes")
        remotes = response.get("remotes", [])
        if not isinstance(remotes, list):
            raise ValueError(f"remotes must be a list, got {remotes!r}")
        return tuple(remotes)

    def oauth_status(self) -> OAuthStatus:
        response = self._rc_client.call("config/oauthstatus")
        return _parse_oauth_status(response)

    def oauth_stop(self) -> None:
        try:
            self._rc_client.call("config/oauthstop")
        except RcCallError as error:
            if str(error.payload.get("error", "")) == _NO_OAUTH_IN_PROGRESS_MESSAGE:
                return
            raise
