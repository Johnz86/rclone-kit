"""
Unit test file.
"""

import os
import unittest

import pytest

from helpers import CLOUD_TEST_KEY_PREFIX, DIGITAL_OCEAN_SPACES_ENV_VARS, skip_if_missing_cloud_env
from rclone_kit import Config, DirListing, Rclone
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


def _generate_rclone_config() -> Config:
    BUCKET_KEY_SECRET = os.getenv("BUCKET_KEY_SECRET")
    BUCKET_KEY_PUBLIC = os.getenv("BUCKET_KEY_PUBLIC")
    BUCKET_URL = "sfo3.digitaloceanspaces.com"

    config_text = f"""
[dst]
type = s3
provider = DigitalOcean
access_key_id = {BUCKET_KEY_PUBLIC}
secret_access_key = {BUCKET_KEY_SECRET}
endpoint = {BUCKET_URL}
"""

    out = Config(config_text)
    return out


@pytest.mark.cloud
class RcloneIsSyncedTests(unittest.TestCase):
    """Test rclone functionality."""

    def setUp(self) -> None:
        """Check if all required environment variables are set before running tests."""
        skip_if_missing_cloud_env(self, DIGITAL_OCEAN_SPACES_ENV_VARS)
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_copydir_then_check_equal(self) -> None:
        """Test copying a single file to remote storage."""
        rclone = Rclone(_generate_rclone_config())
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
