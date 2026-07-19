"""
Unit test file.
"""

import os
import unittest

import pytest
from dotenv import load_dotenv

from helpers import CLOUD_TEST_KEY_PREFIX, DIGITAL_OCEAN_SPACES_ENV_VARS, skip_if_missing_cloud_env
from rclone_kit import (
    CompletedProcess,
    Config,
    DirListing,
    File,
    Rclone,
    rclone_verbose,
)

load_dotenv()
rclone_verbose(True)

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
class RcloneCopyFilesTest(unittest.TestCase):
    """Test rclone functionality."""

    def setUp(self) -> None:
        """Check if all required environment variables are set before running tests."""
        skip_if_missing_cloud_env(self, DIGITAL_OCEAN_SPACES_ENV_VARS)
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_copylist(self) -> None:
        """Test copying a list of files to remote storage."""
        rclone = Rclone(_generate_rclone_config())
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
        rclone = Rclone(_generate_rclone_config())
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
