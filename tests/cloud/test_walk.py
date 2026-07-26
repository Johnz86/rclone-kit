"""
Unit test file.
"""

import os
import unittest

import pytest

from rclone_kit import DirListing, Rclone
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneWalkTest(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_cloud_rclone(self, cloud_rclone: Rclone) -> None:
        self.rclone = cloud_rclone

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_walk(self) -> None:
        rclone = self.rclone

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
        rclone = self.rclone

        dirlisting: DirListing
        for dirlisting in rclone.walk(f"dst:{BUCKET_NAME}", max_depth=1, breadth_first=False):
            print(dirlisting)
        print("done")


if __name__ == "__main__":
    unittest.main()
