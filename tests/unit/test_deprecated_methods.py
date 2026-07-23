"""Unit tests for the Wave I deprecation warnings (C02/C06/C07/C08):
`upgrade_rclone()`, `webgui()`, `launch_server()`, `remote_control()` all
still work exactly as before, but now emit `DeprecationWarning`.
"""

from unittest.mock import MagicMock

import pytest

from rclone_kit.client import Rclone


def _bare_rclone() -> Rclone:
    rclone = object.__new__(Rclone)
    rclone._backend = MagicMock()
    rclone._rc_client = None
    return rclone


def test_upgrade_rclone_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rclone_kit.client.upgrade_rclone", lambda: "fake-path")

    with pytest.warns(DeprecationWarning, match="upgrade_rclone"):
        Rclone.upgrade_rclone()


def test_webgui_warns() -> None:
    rclone = _bare_rclone()
    rclone._launch_process = MagicMock(return_value="fake-process")

    with pytest.warns(DeprecationWarning, match="webgui"):
        rclone.webgui()


def test_launch_server_warns() -> None:
    rclone = _bare_rclone()
    rclone._launch_process = MagicMock(return_value="fake-process")

    with pytest.warns(DeprecationWarning, match="launch_server"):
        rclone.launch_server(addr="localhost:5572")


def test_remote_control_warns() -> None:
    rclone = _bare_rclone()
    fake_cp = MagicMock(returncode=0, stdout="", stderr="")
    rclone._run = MagicMock(return_value=fake_cp)

    with pytest.warns(DeprecationWarning, match="remote_control"):
        rclone.remote_control(addr="localhost:5572")


def test_native_build_info_requires_embedded_execution() -> None:
    from rclone_kit.exceptions import EmbeddedOnlyOperationError

    rclone = _bare_rclone()

    with pytest.raises(EmbeddedOnlyOperationError):
        rclone.native_build_info()
