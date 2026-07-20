"""Unit tests for executable-based rclone configuration discovery."""

import subprocess
from pathlib import Path

import pytest

from helpers import ClientBackendAdapter
from rclone_kit.client import Rclone
from rclone_kit.config_discovery import (
    ConfigDiscoveryError,
    RclonePaths,
    find_conf_file,
    parse_rclone_paths,
)

CONFIG_PATHS_STDOUT = "Config file: {config_file}\nCache dir: {cache_dir}\nTemp dir: {temp_dir}\n"
WINDOWS_CONFIG_PATHS_STDOUT = (
    "Config file: C:\\Users\\example\\rclone.conf\n"
    "Cache dir: C:\\Users\\example\\cache\n"
    "Temp dir: C:\\Users\\example\\temp\n"
)


def _config_paths_stdout(config_file: Path, tmp_path: Path) -> str:
    return CONFIG_PATHS_STDOUT.format(
        config_file=config_file,
        cache_dir=tmp_path / "cache",
        temp_dir=tmp_path / "temp",
    )


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


def test_returns_existing_config_reported_by_rclone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    executable = tmp_path / "rclone"
    existing = tmp_path / "rclone.conf"
    existing.write_text("", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setattr(
        "rclone_kit.config_discovery.get_rclone_exe",
        lambda rclone_exe: rclone_exe,
    )

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert options == {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "check": True,
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_config_paths_stdout(existing, tmp_path),
            stderr="",
        )

    monkeypatch.setattr("rclone_kit.config_discovery.subprocess.run", run)

    assert find_conf_file(rclone_exe=executable) == existing
    assert commands == [[str(executable), "config", "paths"]]


def test_returns_none_when_reported_config_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    missing = tmp_path / "missing.conf"
    paths = RclonePaths(missing, tmp_path / "cache", tmp_path / "temp")

    def report_paths(rclone_exe: Path | None) -> RclonePaths:
        del rclone_exe
        return paths

    monkeypatch.setattr(
        "rclone_kit.config_discovery._config_paths_via_executable",
        report_paths,
    )

    assert find_conf_file(rclone_exe=tmp_path / "rclone") is None


def test_raises_discovery_error_when_rclone_command_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    executable = tmp_path / "rclone"
    monkeypatch.setattr(
        "rclone_kit.config_discovery.get_rclone_exe",
        lambda rclone_exe: rclone_exe,
    )

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, [str(executable), "config", "paths"])

    monkeypatch.setattr("rclone_kit.config_discovery.subprocess.run", run)

    with pytest.raises(ConfigDiscoveryError) as error:
        find_conf_file(rclone_exe=executable)

    assert isinstance(error.value.__cause__, subprocess.CalledProcessError)


def test_raises_discovery_error_when_executable_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rclone_kit.runtime.exceptions import RcloneResolutionError

    monkeypatch.delenv("RCLONE_CONFIG", raising=False)

    def fail_resolution(*_args: object, **_kwargs: object) -> Path:
        raise RcloneResolutionError(["bundled_asset"])

    monkeypatch.setattr("rclone_kit.config_discovery.get_rclone_exe", fail_resolution)

    with pytest.raises(ConfigDiscoveryError) as error:
        find_conf_file()

    assert isinstance(error.value.__cause__, RcloneResolutionError)


def test_parse_rclone_paths_preserves_windows_drive_letters() -> None:
    result = parse_rclone_paths(WINDOWS_CONFIG_PATHS_STDOUT)

    assert result == RclonePaths(
        config_file=Path("C:\\Users\\example\\rclone.conf"),
        cache_dir=Path("C:\\Users\\example\\cache"),
        temp_dir=Path("C:\\Users\\example\\temp"),
    )


def test_parse_rclone_paths_models_omitted_values() -> None:
    result = parse_rclone_paths("Config file: /home/user/.config/rclone/rclone.conf\n")

    assert result == RclonePaths(
        config_file=Path("/home/user/.config/rclone/rclone.conf"),
        cache_dir=None,
        temp_dir=None,
    )


def test_config_show_executes_show_command() -> None:
    rclone = object.__new__(Rclone)
    rclone._backend = ClientBackendAdapter(rclone)
    commands: list[list[str]] = []

    def run(
        cmd: list[str],
        check: bool = False,
        capture: bool | Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert capture is True
        assert check is True
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="[remote]\ntype = s3\n", stderr="")

    rclone._run = run

    result = rclone.config_show(remote="remote", obscure=True)

    assert result == "[remote]\ntype = s3\n"
    assert commands == [["config", "show", "remote", "--obscure"]]


def test_config_show_rejects_conflicting_secret_options() -> None:
    rclone = object.__new__(Rclone)
    rclone._backend = ClientBackendAdapter(rclone)

    with pytest.raises(ValueError):
        rclone.config_show(obscure=True, no_obscure=True)
