"""
Unit test file.
"""

import os
import unittest

import pytest

from helpers import DIGITAL_OCEAN_SPACES_ENV_VARS, skip_if_missing_cloud_env
from rclone_kit import Config, DirListing, Rclone, SizeResult
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
class RcloneSizeFilesTester(unittest.TestCase):
    """Test rclone functionality."""

    def setUp(self) -> None:
        """Check if all required environment variables are set before running tests."""
        skip_if_missing_cloud_env(self, DIGITAL_OCEAN_SPACES_ENV_VARS)
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_size(self) -> None:
        rclone = Rclone(_generate_rclone_config())
        # rclone.walk
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
        size_map: SizeResult | Exception = rclone.size_files(src=src, files=files, check=True)
        if isinstance(size_map, Exception):
            self.fail(size_map)
        print(size_map)
        print("done")


if __name__ == "__main__":
    unittest.main()
