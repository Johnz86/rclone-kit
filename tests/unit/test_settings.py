"""Unit tests for `rclone_kit.settings`: the `LogSettings` accessors and the
environment variables backing them."""

import pytest

from rclone_kit.settings import (
    _RCLONE_VERBOSE_ENV_VAR,
    _UPLOAD_PARTS_LOGGING_ENV_VAR,
    LogSettings,
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_RCLONE_VERBOSE_ENV_VAR, "0")
    monkeypatch.delenv(_UPLOAD_PARTS_LOGGING_ENV_VAR, raising=False)


def test_log_settings_rclone_verbose_round_trips_through_the_environment() -> None:
    assert LogSettings.rclone_verbose(True) is True
    assert LogSettings.rclone_verbose() is True
    assert LogSettings.rclone_verbose(False) is False


def test_upload_parts_logging_round_trips_through_the_environment() -> None:
    assert LogSettings.enable_upload_parts_logging() is False
    assert LogSettings.enable_upload_parts_logging(True) is True
    assert LogSettings.enable_upload_parts_logging(False) is False
