"""Unit tests for the credential-bearing temporary `rclone.conf` that
`rclone_kit.util.make_temp_config_file` hands to the native runtime."""

import os
import shutil
import stat

import pytest

from rclone_kit.util import make_temp_config_file


def test_temporary_config_lives_in_its_own_private_directory() -> None:
    config_path = make_temp_config_file()
    try:
        assert config_path.exists()
        assert list(config_path.parent.iterdir()) == [config_path]
    finally:
        shutil.rmtree(config_path.parent, ignore_errors=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
def test_temporary_config_has_owner_only_permissions() -> None:
    config_path = make_temp_config_file()
    try:
        mode = stat.S_IMODE(config_path.stat().st_mode)
        assert mode == stat.S_IRUSR | stat.S_IWUSR
    finally:
        shutil.rmtree(config_path.parent, ignore_errors=True)
