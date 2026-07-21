"""Live test file.

Exercises `Rclone` construction from the real Drive config file and proves
the S3-optimized path's provider boundary against a real non-S3 backend,
not just the fake-config unit tests in `tests/unit/test_config_ops.py`.
"""

from pathlib import Path

import pytest

from rclone_kit import Rclone

pytestmark = pytest.mark.live_gdrive


def test_listremotes_reports_the_configured_remote(
    live_rclone: Rclone, live_remote_name: str
) -> None:
    remote_names = [remote.name for remote in live_rclone.listremotes()]

    assert live_remote_name in remote_names


def test_configured_remote_is_not_s3(live_rclone: Rclone, live_test_root: str) -> None:
    assert live_rclone.is_s3(f"{live_test_root}/probe-path.txt") is False


def test_get_s3_credentials_rejects_a_drive_remote(
    live_rclone: Rclone, live_test_root: str
) -> None:
    """`get_s3_credentials()` needs a full object path, not a bare remote
    name - `S3PathInfo.from_str` requires a bucket *and* a key, checked
    before the provider-type rejection this test is actually after."""
    with pytest.raises(ValueError, match="is not an S3 remote"):
        live_rclone.get_s3_credentials(f"{live_test_root}/probe-path.txt")


def test_copy_file_s3_rejects_a_drive_destination(
    live_rclone: Rclone, live_test_root: str, tmp_path: Path
) -> None:
    local_file = tmp_path / "direct.bin"
    local_file.write_bytes(b"payload")

    with pytest.raises(ValueError, match="Destination is not an S3 remote"):
        live_rclone.copy_file_s3(src=local_file, dst=f"{live_test_root}/direct.bin")


# copy_file_s3_resumable() is deliberately not covered here: unlike
# copy_file_s3()/get_s3_credentials(), it does not check the destination's
# provider up front - verified directly against this remote, it fails on
# an unrelated part-size or file-existence check first depending on the
# arguments given, not a clean, fast "not an S3 remote" rejection.
