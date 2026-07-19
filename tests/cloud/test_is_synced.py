"""
Unit test file.
"""

import os
import unittest

import pytest

from helpers import CLOUD_TEST_KEY_PREFIX
from rclone_kit import Config, DirListing, Rclone
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneIsSyncedTests(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_do_spaces_config(self, do_spaces_config: Config) -> None:
        self.config = do_spaces_config

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_copydir_then_check_equal(self) -> None:
        """Test copying a single file to remote storage."""
        rclone = Rclone(self.config)
        path = f"dst:{BUCKET_NAME}/zachs_video"
        listing: DirListing = rclone.ls(path)
        self.assertGreater(len(listing.dirs), 0)
        src_dir = listing.dirs[0]
        dst_dir = f"dst:{BUCKET_NAME}/{CLOUD_TEST_KEY_PREFIX}is_synced_test"
        rclone.purge(dst_dir)
        try:
            is_synced = rclone.is_synced(src_dir, dst_dir)
            self.assertFalse(is_synced)
            rclone.copy_dir(src_dir, dst_dir)
            is_synced = rclone.is_synced(src_dir, dst_dir)
            self.assertTrue(is_synced)
        finally:
            rclone.purge(dst_dir)


if __name__ == "__main__":
    unittest.main()
