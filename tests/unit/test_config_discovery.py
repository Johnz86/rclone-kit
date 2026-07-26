"""Unit tests for rclone config-path parsing and discovery precedence."""

from pathlib import Path

import pytest

from rclone_kit.config_discovery import RclonePaths, find_conf_file_embedded, parse_rclone_paths

WINDOWS_CONFIG_PATHS_STDOUT = (
    "Config file: C:\\Users\\example\\rclone.conf\n"
    "Cache dir: C:\\Users\\example\\cache\n"
    "Temp dir: C:\\Users\\example\\temp\n"
)


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


def test_find_conf_file_embedded_explicit_path_wins_over_environment_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "should-not-be-used.conf"))
    explicit = tmp_path / "explicit.conf"

    assert find_conf_file_embedded(explicit_path=explicit) == explicit


def test_find_conf_file_embedded_env_var_wins_when_no_explicit_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_path = tmp_path / "env.conf"
    monkeypatch.setenv("RCLONE_CONFIG", str(env_path))

    assert find_conf_file_embedded() == env_path
