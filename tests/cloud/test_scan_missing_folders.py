"""
Unit test file.
"""

import os
import unittest

import pytest

from helpers import DIGITAL_OCEAN_SPACES_ENV_VARS, skip_if_missing_cloud_env
from rclone_kit import Config, Dir, Rclone
from rclone_kit.env_file import load_env_file
from rclone_kit.types import Order

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
class RcloneScanMissingFoldersTests(unittest.TestCase):
    """Test rclone functionality."""

    def setUp(self) -> None:
        """Check if all required environment variables are set before running tests."""
        skip_if_missing_cloud_env(self, DIGITAL_OCEAN_SPACES_ENV_VARS)
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    @unittest.skip("Skip test")
    def test_scan_missing_folders(self) -> None:
        """Test copying a single file to remote storage."""
        rclone = Rclone(_generate_rclone_config())
        all: list[Dir] = list(
            rclone.scan_missing_folders(
                src="dst:rclone-kit-unit-test",
                dst="dst:rclone-kit-unit-test",
                max_depth=-1,
                order=Order.NORMAL,
            )
        )
        self.assertEqual(len(all), 0)
        msg = "\n".join([str(item) for item in all])
        print(msg)


if __name__ == "__main__":
    unittest.main()
