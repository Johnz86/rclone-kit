"""Unit tests for `rclone_kit.runtime.cache_dir`.

Each branch (Windows/Linux, env var set/unset) has its own standalone test
rather than a shared parametrization, since the setup for each combination
(which env var is set vs. deleted, which fallback path is expected) differs
enough that a shared case shape would obscure more than it clarifies.
"""

from pathlib import Path

import pytest

from rclone_kit.runtime.cache_dir import user_cache_dir
from rclone_kit.runtime.platform import OperatingSystem

_APP_NAME = "rclone-kit-test"


def test_windows_cache_dir_uses_localappdata_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\example\AppData\Local")

    result = user_cache_dir(_APP_NAME, operating_system=OperatingSystem.WINDOWS)

    assert result == Path(r"C:\Users\example\AppData\Local") / _APP_NAME


def test_windows_cache_dir_falls_back_to_home_when_localappdata_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    result = user_cache_dir(_APP_NAME, operating_system=OperatingSystem.WINDOWS)

    assert result == Path.home() / "AppData" / "Local" / _APP_NAME


def test_linux_cache_dir_uses_xdg_cache_home_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/example/.cache")

    result = user_cache_dir(_APP_NAME, operating_system=OperatingSystem.LINUX)

    assert result == Path("/home/example/.cache") / _APP_NAME


def test_linux_cache_dir_falls_back_to_home_when_xdg_cache_home_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    result = user_cache_dir(_APP_NAME, operating_system=OperatingSystem.LINUX)

    assert result == Path.home() / ".cache" / _APP_NAME


def test_default_operating_system_resolves_without_error() -> None:
    result = user_cache_dir(_APP_NAME)

    assert result.name == _APP_NAME
