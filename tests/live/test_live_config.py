"""Live test file.

Exercises `Rclone` construction from the real config file and the small set
of read-only introspection methods every other live test relies on.
"""

import pytest

from conftest import LIVE_REMOTE, LIVE_TEST_ROOT
from rclone_kit import Rclone

pytestmark = pytest.mark.live


def test_listremotes_reports_the_configured_remote(live_rclone: Rclone) -> None:
    remote_names = [remote.name for remote in live_rclone.listremotes()]

    assert LIVE_REMOTE in remote_names


def test_configured_remote_is_s3(live_rclone: Rclone) -> None:
    """`is_s3()` needs a full object path, not a bare bucket root -
    `S3PathInfo.from_str` requires a bucket *and* a key."""
    assert live_rclone.is_s3(f"{LIVE_TEST_ROOT}/probe-path.txt") is True


def test_config_paths_reports_the_real_config_file(live_rclone: Rclone) -> None:
    config_path, cache_path, temp_path = live_rclone.config_paths()

    assert config_path.is_file()
    assert cache_path.is_dir()
    assert temp_path.is_dir()
