"""
Unit test file.
"""

import os
import unittest

import pytest

from rclone_kit import Config, DirListing, Rclone
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneCopyTests(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_do_spaces_config(self, do_spaces_config: Config) -> None:
        self.config = do_spaces_config

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_copyfile(self) -> None:
        """Test copying a single file to remote storage."""
        rclone = Rclone(self.config)
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
        rclone = Rclone(self.config)
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
