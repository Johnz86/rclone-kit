"""
Unit test file.
"""

import os
import unittest

import pytest
from dotenv import load_dotenv

from helpers import DIGITAL_OCEAN_SPACES_ENV_VARS, skip_if_missing_cloud_env
from rclone_kit import Config, DirListing, Rclone

load_dotenv()

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
class RcloneCopyTests(unittest.TestCase):
    """Test rclone functionality."""

    def setUp(self) -> None:
        """Check if all required environment variables are set before running tests."""
        skip_if_missing_cloud_env(self, DIGITAL_OCEAN_SPACES_ENV_VARS)
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_copyfile(self) -> None:
        """Test copying a single file to remote storage."""
        rclone = Rclone(_generate_rclone_config())
        path = f"dst:{BUCKET_NAME}/zachs_video"
        listing: DirListing = rclone.ls(path, glob="*.png")
        self.assertGreater(len(listing.files), 0)
        file = listing.files[0]

        new_name = file.name + "_copy"
        new_path = f"dst:{BUCKET_NAME}/zachs_video/{new_name}"
        try:
            rclone.copy_to(file, new_path)
            listing = rclone.ls(f"dst:{BUCKET_NAME}/zachs_video/", glob=f"*{new_name}")
            self.assertEqual(len(listing.files), 1)
            self.assertEqual(listing.dirs, [])
        finally:
            rclone.delete_files([new_path])

    def test_copyfiles(self) -> None:
        """Test copying multiple files to remote storage."""
        rclone = Rclone(_generate_rclone_config())
        path = f"dst:{BUCKET_NAME}/zachs_video"
        listing: DirListing = rclone.ls(path, glob="*.png")
        self.assertGreater(len(listing.files), 0)
        first_file = str(listing.files[0])
        dest_file = first_file + "_copy"

        try:
            rclone.copy_to(first_file, dest_file)
            exists = rclone.exists(dest_file)
            self.assertTrue(exists)
        finally:
            rclone.delete_files(dest_file)


if __name__ == "__main__":
    unittest.main()
