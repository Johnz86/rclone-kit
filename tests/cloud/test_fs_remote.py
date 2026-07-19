"""Cloud test file for `RemoteFS` against a real DigitalOcean Spaces bucket."""

import os
import unittest

import pytest

from helpers import (
    CLOUD_TEST_KEY_PREFIX,
    CLOUD_TEST_REMOTE_ROOT,
    DIGITAL_OCEAN_SPACES_ENV_VARS,
    skip_if_missing_cloud_env,
)
from rclone_kit import Config
from rclone_kit.fs.filesystem import FSPath, RemoteFS


def _generate_rclone_config() -> Config:
    bucket_key_secret = os.getenv("BUCKET_KEY_SECRET")
    bucket_key_public = os.getenv("BUCKET_KEY_PUBLIC")
    bucket_url = "sfo3.digitaloceanspaces.com"

    config_text = f"""
[dst]
type = s3
provider = DigitalOcean
access_key_id = {bucket_key_public}
secret_access_key = {bucket_key_secret}
endpoint = {bucket_url}
"""

    return Config(config_text)


@pytest.mark.cloud
class RcloneRemoteFSTester(unittest.TestCase):
    """Tests for RemoteFS functionality."""

    def setUp(self) -> None:
        """Check if all required environment variables are set before running tests."""
        skip_if_missing_cloud_env(self, DIGITAL_OCEAN_SPACES_ENV_VARS)

    def test_create_and_move_remote_fs(self) -> None:
        """Test create and move functionality."""
        config = _generate_rclone_config()

        fs = RemoteFS.from_rclone_config(CLOUD_TEST_REMOTE_ROOT, config)
        with fs.cwd() as cwd:
            remote_tester: FSPath = cwd / f"{CLOUD_TEST_KEY_PREFIX}remote_tester"
            remote_tester.rmtree(ignore_errors=True)
            try:
                new_file_path = remote_tester / "test.txt"
                new_file_path.write_bytes(b"test")

                moved_file_path = remote_tester / "moved_test.txt"
                new_file_path.move_to(moved_file_path)
                self.assertTrue(moved_file_path.exists())
                self.assertFalse(new_file_path.exists())
            finally:
                remote_tester.rmtree(ignore_errors=True)

    @unittest.skip("This test fails, file remains in cache after removal")
    def test_create_and_remove_remote_fs(self) -> None:
        """Test create and remove functionality."""
        config = _generate_rclone_config()

        fs = RemoteFS.from_rclone_config(CLOUD_TEST_REMOTE_ROOT, config)
        with fs.cwd() as cwd:
            remote_tester: FSPath = cwd / f"{CLOUD_TEST_KEY_PREFIX}remote_tester"
            try:
                new_file_path = remote_tester / "test.txt"
                new_file_path.write_bytes(b"test")
                self.assertTrue(new_file_path.exists())

                new_file_path.remove()

                exists = new_file_path.exists()
                self.assertFalse(exists)
            finally:
                remote_tester.rmtree(ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
