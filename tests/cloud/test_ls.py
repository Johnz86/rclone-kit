"""
Unit test file.
"""

import os
import unittest

import pytest

from rclone_kit import Config, Dir, DirListing, File, Rclone, Remote
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneLsTests(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_do_spaces_config(self, do_spaces_config: Config) -> None:
        self.config = do_spaces_config

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_list_remotes(self) -> None:
        rclone = Rclone(self.config)

        remotes: list[Remote] = rclone.listremotes()
        self.assertGreater(len(remotes), 0)
        for remote in remotes:
            self.assertIsInstance(remote, Remote)
            print(remote)
        print("done")

    def test_ls_root(self) -> None:
        """Test listing the root directory of the bucket.

        Verifies that we can:
        1. Connect to the bucket
        2. List its contents
        3. Get both directories and files as proper types
        """
        self.assertIsNotNone(BUCKET_NAME)
        rclone = Rclone(self.config)
        listing: DirListing = rclone.ls(f"dst:{BUCKET_NAME}", max_depth=-1)

        self.assertIsInstance(listing, DirListing)
        self.assertGreater(len(listing.dirs), 0)
        self.assertGreater(len(listing.files), 0)

        for dir in listing.dirs:
            self.assertIsInstance(dir, Dir)
            print(dir)

        for file in listing.files:
            self.assertIsInstance(file, File)
            print(file)

        print("done")

    def test_ls_subdir(self) -> None:
        rclone = Rclone(self.config)
        path = f"dst:{BUCKET_NAME}/zachs_video"
        listing: DirListing = rclone.ls(path)
        print(listing)

    def test_ls_glob_png(self) -> None:
        rclone = Rclone(self.config)
        path = f"dst:{BUCKET_NAME}/zachs_video"
        listing: DirListing = rclone.ls(path, glob="*.png")
        self.assertGreater(len(listing.files), 0)
        for file in listing.files:
            self.assertIsInstance(file, File)

            self.assertTrue(file.name.endswith(".png"))

        self.assertEqual(len(listing.dirs), 0)


if __name__ == "__main__":
    unittest.main()
