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


@pytest.mark.cloud
@pytest.mark.mount
class RcloneMountTests(unittest.TestCase):
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

    @unittest.skip(
        "Manual test: requires a real OS mount facility (FUSE on Linux, WinFsp on "
        "Windows) plus a live bucket; not covered by a deterministic fake. Run "
        "manually when validating mount behavior."
    )
    def test_mount(self) -> None:
        """Test mounting a remote bucket."""
        remote_path = f"dst:{self.bucket_name}"

        handle = self.rclone.mount(remote_path, self.mount_point)
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
