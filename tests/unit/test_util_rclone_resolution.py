"""Unit tests for the `rclone_kit.util` wrappers around the runtime resolver
and the temporary-config/signal-cleanup helpers.

`resolve_rclone_executable` itself is exercised in
`test_runtime_rclone_binary.py`; these tests only check that `util.py`
forwards the right arguments and keeps its filesystem/signal boundaries
correct. No test spawns a real rclone process.
"""

import threading
from pathlib import Path
from typing import Any

import pytest

from rclone_kit import util


def test_get_rclone_exe_forwards_explicit_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def fake_resolve(**kwargs: Any) -> Path:
        captured.update(kwargs)
        return kwargs["explicit_path"]

    monkeypatch.setattr(util, "resolve_rclone_executable", fake_resolve)
    explicit = tmp_path / "rclone"

    result = util.get_rclone_exe(explicit)

    assert result == explicit
    assert captured["explicit_path"] == explicit


def test_get_rclone_exe_default_allows_path_lookup_but_not_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_resolve(**kwargs: Any) -> Path:
        captured.update(kwargs)
        return Path("resolved")

    monkeypatch.setattr(util, "resolve_rclone_executable", fake_resolve)

    util.get_rclone_exe(None)

    assert captured["allow_path_lookup"] is True
    assert captured["allow_verified_download"] is False


def test_get_rclone_exe_can_require_bundled_executable_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_resolve(**kwargs: Any) -> Path:
        captured.update(kwargs)
        return Path("resolved")

    monkeypatch.setattr(util, "resolve_rclone_executable", fake_resolve)

    util.get_rclone_exe(None, allow_path_lookup=False)

    assert captured["allow_path_lookup"] is False


def test_upgrade_rclone_requests_verified_download(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_resolve(**kwargs: Any) -> Path:
        captured.update(kwargs)
        return Path("resolved")

    monkeypatch.setattr(util, "resolve_rclone_executable", fake_resolve)

    util.upgrade_rclone()

    assert captured["allow_verified_download"] is True


def test_register_signal_cleanup_rejects_non_main_thread() -> None:
    errors: list[Exception] = []

    def target() -> None:
        try:
            util.register_signal_cleanup()
        except RuntimeError as error:
            errors.append(error)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()

    assert len(errors) == 1


def test_make_temp_config_file_is_outside_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    config_path = util.make_temp_config_file()
    try:
        assert tmp_path not in config_path.parents
        assert config_path.name == "rclone.conf"
    finally:
        util.clear_temp_config_file(config_path)
        assert not config_path.parent.exists()


def test_clear_temp_config_file_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "rclone.conf"
    path.write_text("", encoding="utf-8")

    util.clear_temp_config_file(path)
    util.clear_temp_config_file(path)

    assert not path.exists()
