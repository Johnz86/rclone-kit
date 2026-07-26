"""
Unit test file for testing rclone mount functionality.
"""

import os
import unittest
from pathlib import Path

import pytest

from rclone_kit import Rclone
from rclone_kit.env_file import load_env_file

load_env_file()

# Manual test: requires a real OS mount facility (FUSE on Linux, WinFsp on
# Windows) plus a live bucket; not covered by a deterministic fake. Flip to
# True locally to run it when validating S3-tuned mount behavior.
_ENABLED = False


@pytest.mark.cloud
@pytest.mark.mount
class RcloneMountS3Tests(unittest.TestCase):
    """Test rclone mount functionality."""

    @pytest.fixture(autouse=True)
    def _inject_cloud_rclone(self, cloud_rclone: Rclone) -> None:
        self.rclone = cloud_rclone

    def setUp(self) -> None:
        self.bucket_name = os.getenv("BUCKET_NAME")
        self.mount_point = Path("test_mount")

        parent = self.mount_point.parent
        if not parent.exists():
            parent.mkdir(parents=True)

        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    @unittest.skipUnless(_ENABLED, "Disabled by default; see _ENABLED above")
    def test_mount(self) -> None:
        """Test mounting a remote bucket."""
        remote_path = f"dst:{self.bucket_name}"

        handle = self.rclone.mount_s3(remote_path, self.mount_point)
        try:
            self.assertFalse(handle.closed)
            self.assertTrue(self.mount_point.exists())
            self.assertTrue(self.mount_point.is_dir())

            contents = list(self.mount_point.iterdir())
            self.assertGreater(len(contents), 0, "Mounted directory should not be empty")
        finally:
            handle.dispose()


if __name__ == "__main__":
    unittest.main()
