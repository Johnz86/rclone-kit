"""
Unit test file.
"""

import os
import unittest

import pytest

from helpers import DIGITAL_OCEAN_SPACES_ENV_VARS, skip_if_missing_cloud_env
from rclone_kit import Config, Rclone, Remote
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
class RcloneRemoteTests(unittest.TestCase):
    """Test rclone functionality."""

    def setUp(self) -> None:
        """Check if all required environment variables are set before running tests."""
        skip_if_missing_cloud_env(self, DIGITAL_OCEAN_SPACES_ENV_VARS)
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_list_remotes(self) -> None:
        rclone = Rclone(_generate_rclone_config())

        remotes: list[Remote] = rclone.listremotes()
        self.assertGreater(len(remotes), 0)
        for remote in remotes:
            self.assertIsInstance(remote, Remote)
            print(remote)
        print("done")


if __name__ == "__main__":
    unittest.main()
