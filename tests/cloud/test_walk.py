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
class RcloneWalkTest(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_do_spaces_config(self, do_spaces_config: Config) -> None:
        self.config = do_spaces_config

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_walk(self) -> None:
        rclone = Rclone(self.config)

        dirlisting: DirListing
        is_first = True
        for dirlisting in rclone.walk(f"dst:{BUCKET_NAME}", max_depth=1):
            if is_first:
                self.assertGreaterEqual(len(dirlisting.files), 1)

                self.assertEqual(dirlisting.files[0].name, "first.txt")
                is_first = False
            print(dirlisting)
        print("done")

    def test_walk_depth_first(self) -> None:
        rclone = Rclone(self.config)

        dirlisting: DirListing
        for dirlisting in rclone.walk(f"dst:{BUCKET_NAME}", max_depth=1, breadth_first=False):
            print(dirlisting)
        print("done")


if __name__ == "__main__":
    unittest.main()
