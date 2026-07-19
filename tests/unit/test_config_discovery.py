"""Unit tests for `rclone_kit.config` configuration-file discovery.

No test spawns a real rclone process; the `rclone` argument is either
omitted (exercising only the environment-variable and explicit-path steps)
or a bare `RcloneImpl` instance with a monkeypatched `config_paths` method.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from rclone_kit import config as config_module
from rclone_kit.config import ConfigDiscoveryError, find_conf_file
from rclone_kit.rclone_impl import RcloneImpl
from rclone_kit.rclone_impl import _parse_paths as parse_all_config_paths


def _make_bare_rclone_impl(config_paths_result: Any) -> RcloneImpl:
    instance = object.__new__(RcloneImpl)
    instance.config_paths = (  # type: ignore[method-assign]
        lambda _remote=None, _obscure=False, _no_obscure=False: config_paths_result
    )
    return instance


def test_explicit_path_wins_over_environment_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "should-not-be-used.conf"))
    explicit = tmp_path / "explicit.conf"

    assert find_conf_file(explicit_path=explicit) == explicit


def test_rclone_config_env_var_wins_when_no_explicit_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_path = tmp_path / "env.conf"
    monkeypatch.setenv("RCLONE_CONFIG", str(env_path))

    assert find_conf_file() == env_path


def test_returns_none_when_reported_config_file_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    rclone_impl = _make_bare_rclone_impl([tmp_path / "missing.conf"])

    assert find_conf_file(rclone_impl) is None


def test_returns_existing_reported_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    existing = tmp_path / "rclone.conf"
    existing.write_text("", encoding="utf-8")
    rclone_impl = _make_bare_rclone_impl([existing, tmp_path / "cache", tmp_path / "temp"])

    assert find_conf_file(rclone_impl) == existing


def test_raises_config_discovery_error_when_config_paths_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    rclone_impl = _make_bare_rclone_impl(RuntimeError("boom"))

    with pytest.raises(ConfigDiscoveryError):
        find_conf_file(rclone_impl)


def test_rejects_wrong_typed_rclone_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)

    with pytest.raises(TypeError):
        find_conf_file(rclone="not-an-rclone-instance")  # type: ignore[arg-type]


def test_parse_config_paths_output_extracts_only_config_file_line() -> None:
    stdout = (
        "Config file: /home/user/.config/rclone/rclone.conf\n"
        "Cache dir:   /home/user/.cache/rclone\n"
        "Temp dir:    /tmp\n"
    )

    result = config_module._parse_config_paths_output(stdout)

    assert result == [Path("/home/user/.config/rclone/rclone.conf")]


def test_parse_all_config_paths_preserves_windows_drive_letters() -> None:
    stdout = (
        "Config file: C:\\Users\\example\\rclone.conf\n"
        "Cache dir: C:\\Users\\example\\cache\n"
        "Temp dir: C:\\Users\\example\\temp\n"
    )

    result = parse_all_config_paths(stdout)

    assert result == [
        Path("C:\\Users\\example\\rclone.conf"),
        Path("C:\\Users\\example\\cache"),
        Path("C:\\Users\\example\\temp"),
    ]


def test_config_show_executes_show_command() -> None:
    rclone_impl = object.__new__(RcloneImpl)
    commands: list[list[str]] = []

    def run(
        command: list[str],
        capture: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert capture is True
        assert check is True
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="[remote]\ntype = s3\n", stderr="")

    rclone_impl._run = run  # type: ignore[method-assign]

    result = rclone_impl.config_show(remote="remote", obscure=True)

    assert result == "[remote]\ntype = s3\n"
    assert commands == [["config", "show", "remote", "--obscure"]]


def test_config_show_rejects_conflicting_secret_options() -> None:
    rclone_impl = object.__new__(RcloneImpl)

    with pytest.raises(ValueError):
        rclone_impl.config_show(obscure=True, no_obscure=True)


def test_config_paths_via_resolved_executable_raises_when_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rclone_kit.runtime.exceptions import RcloneResolutionError

    def fail_resolution(*_args: object, **_kwargs: object) -> Path:
        raise RcloneResolutionError(["bundled_asset"])

    monkeypatch.setattr("rclone_kit.util.get_rclone_exe", fail_resolution)

    with pytest.raises(ConfigDiscoveryError):
        config_module._config_paths_via_resolved_executable()
