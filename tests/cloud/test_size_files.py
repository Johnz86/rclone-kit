"""
Unit test file.
"""

import os
import unittest

import pytest

from rclone_kit import DirListing, Rclone, SizeResult
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneSizeFilesTester(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_cloud_rclone(self, cloud_rclone: Rclone) -> None:
        self.rclone = cloud_rclone

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_size(self) -> None:
        rclone = self.rclone

        dirlisting: DirListing
        is_first = True
        files: list[str] = []
        src = f"dst:{BUCKET_NAME}"
        for dirlisting in rclone.walk(src, max_depth=1):
            if is_first:
                self.assertGreaterEqual(len(dirlisting.files), 1)
                self.assertEqual(dirlisting.files[0].name, "first.txt")
                is_first = False
            files.extend(dirlisting.files_relative(src))
        size_map: SizeResult = rclone.size_files(src=src, files=files, check=True)
        print(size_map)
        print("done")


if __name__ == "__main__":
    unittest.main()
