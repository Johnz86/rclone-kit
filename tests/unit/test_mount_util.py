"""Unit tests for `rclone_kit.mount_util` mount-prerequisite detection.

No test mounts a filesystem or depends on WinFsp/FUSE actually being
installed on the host; `_SYSTEM` and the filesystem/`PATH` checks are
patched at the module boundary.
"""

from pathlib import Path

import pytest

from rclone_kit import mount_util
from rclone_kit.mount_util import (
    MountPrerequisiteError,
    ensure_mount_supported,
    is_fuse_available,
    is_winfsp_available,
)


def test_is_winfsp_available_false_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mount_util, "_SYSTEM", "Linux")

    assert is_winfsp_available() is False


def test_is_winfsp_available_true_when_dll_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mount_util, "_SYSTEM", "Windows")
    dll_path = tmp_path / "WinFsp" / "bin" / "winfsp-x64.dll"
    dll_path.parent.mkdir(parents=True)
    dll_path.write_bytes(b"")

    assert is_winfsp_available(program_files_dirs=(tmp_path,)) is True


def test_is_winfsp_available_false_when_dll_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mount_util, "_SYSTEM", "Windows")

    assert is_winfsp_available(program_files_dirs=(tmp_path,)) is False


def test_is_fuse_available_false_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mount_util, "_SYSTEM", "Windows")

    assert is_fuse_available() is False


def test_is_fuse_available_true_when_device_and_command_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mount_util, "_SYSTEM", "Linux")
    fake_device = tmp_path / "fuse"
    fake_device.write_bytes(b"")
    monkeypatch.setattr(mount_util.shutil, "which", lambda _name: "/usr/bin/fusermount")

    assert is_fuse_available(fuse_device_path=fake_device) is True


def test_is_fuse_available_false_when_device_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mount_util, "_SYSTEM", "Linux")
    monkeypatch.setattr(mount_util.shutil, "which", lambda _name: "/usr/bin/fusermount")

    assert is_fuse_available(fuse_device_path=tmp_path / "does-not-exist") is False


def test_is_fuse_available_false_when_no_unmount_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mount_util, "_SYSTEM", "Linux")
    fake_device = tmp_path / "fuse"
    fake_device.write_bytes(b"")
    monkeypatch.setattr(mount_util.shutil, "which", lambda _name: None)

    assert is_fuse_available(fuse_device_path=fake_device) is False


def test_ensure_mount_supported_raises_on_windows_without_winfsp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mount_util, "_SYSTEM", "Windows")
    monkeypatch.setattr(mount_util, "_real_program_files_dirs", lambda: (tmp_path,))

    with pytest.raises(MountPrerequisiteError):
        ensure_mount_supported()


def test_ensure_mount_supported_raises_on_linux_without_fuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mount_util, "_SYSTEM", "Linux")
    monkeypatch.setattr(mount_util, "_LINUX_FUSE_DEVICE_PATH", Path("/definitely/does/not/exist"))

    with pytest.raises(MountPrerequisiteError):
        ensure_mount_supported()


def test_ensure_mount_supported_is_a_noop_on_unrecognized_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mount_util, "_SYSTEM", "Darwin")

    ensure_mount_supported()


def test_run_command_returns_negative_one_when_executable_missing() -> None:
    returncode = mount_util._run_command(["rclone-kit-definitely-not-a-real-command"], False)

    assert returncode == -1
