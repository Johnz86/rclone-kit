"""
Unit test file.
"""

import os
import unittest

import pytest

from helpers import CLOUD_TEST_KEY_PREFIX
from rclone_kit import (
    CompletedProcess,
    Config,
    DirListing,
    File,
    Rclone,
    rclone_verbose,
)
from rclone_kit.env_file import load_env_file

load_env_file()
rclone_verbose(True)

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneCopyFilesTest(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_do_spaces_config(self, do_spaces_config: Config) -> None:
        self.config = do_spaces_config

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_copylist(self) -> None:
        """Test copying a list of files to remote storage."""
        rclone = Rclone(self.config)
        dst_prefix = f"dst:{BUCKET_NAME}/{CLOUD_TEST_KEY_PREFIX}test_out"
        src_prefix = f"dst:{BUCKET_NAME}/zachs_video"
        listing: DirListing = rclone.ls(src_prefix, glob="*.png")
        self.assertGreater(len(listing.files), 0)
        first_file: File = listing.files[0]
        include_files: list[str] = [first_file.name]
        try:
            completed_procs: list[CompletedProcess] = rclone.copy_files(
                src=src_prefix, dst=dst_prefix, files=include_files, max_partition_workers=2
            )
            self.assertGreater(len(completed_procs), 0)
            for proc in completed_procs:
                self.assertTrue(proc.returncode == 0)
            self.assertTrue(rclone.exists(dst_prefix))
        finally:
            rclone.purge(dst_prefix)

    def test_copylist_one_worker(self) -> None:
        """Test copying a list of files to remote storage."""
        rclone = Rclone(self.config)
        dst_prefix = f"dst:{BUCKET_NAME}/{CLOUD_TEST_KEY_PREFIX}test_out"
        src_prefix = f"dst:{BUCKET_NAME}/zachs_video"
        listing: DirListing = rclone.ls(src_prefix, glob="*.png")
        self.assertGreater(len(listing.files), 0)
        first_file: File = listing.files[0]
        include_files: list[str] = [first_file.name]
        try:
            completed_procs: list[CompletedProcess] = rclone.copy_files(
                src=src_prefix, dst=dst_prefix, files=include_files, max_partition_workers=1
            )
            self.assertGreater(len(completed_procs), 0)
            for proc in completed_procs:
                self.assertTrue(proc.returncode == 0)
            self.assertTrue(rclone.exists(dst_prefix))
        finally:
            rclone.purge(dst_prefix)


if __name__ == "__main__":
    unittest.main()
