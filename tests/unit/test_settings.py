"""Unit tests for `rclone_kit.settings`, covering the deprecated
module-level `rclone_verbose` alias and the `LogSettings` API it delegates
to."""

import warnings

import pytest

from rclone_kit.settings import (
    _RCLONE_VERBOSE_ENV_VAR,
    _UPLOAD_PARTS_LOGGING_ENV_VAR,
    LogSettings,
    rclone_verbose,
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_RCLONE_VERBOSE_ENV_VAR, "0")
    monkeypatch.delenv(_UPLOAD_PARTS_LOGGING_ENV_VAR, raising=False)


def test_log_settings_rclone_verbose_round_trips_through_the_environment() -> None:
    assert LogSettings.rclone_verbose(True) is True
    assert LogSettings.rclone_verbose() is True
    assert LogSettings.rclone_verbose(False) is False


def test_log_settings_rclone_verbose_is_not_deprecated() -> None:
    with warnings.catch_warnings(action="error", category=DeprecationWarning):
        assert LogSettings.rclone_verbose(True) is True


def test_deprecated_rclone_verbose_warns_and_still_sets_the_environment() -> None:
    with pytest.deprecated_call():
        assert rclone_verbose(True) is True

    assert LogSettings.rclone_verbose() is True


def test_upload_parts_logging_round_trips_through_the_environment() -> None:
    assert LogSettings.enable_upload_parts_logging() is False
    assert LogSettings.enable_upload_parts_logging(True) is True
    assert LogSettings.enable_upload_parts_logging(False) is False
