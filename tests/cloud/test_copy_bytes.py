"""
Unit test file.
"""

import os
import tempfile
import unittest
from pathlib import Path

import pytest

from helpers import DIGITAL_OCEAN_SPACES_ENV_VARS, skip_if_missing_cloud_env
from rclone_kit import Config, Rclone
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


def _generate_rclone_config() -> Config:
    BUCKET_KEY_SECRET = os.getenv("BUCKET_KEY_SECRET")
    BUCKET_KEY_PUBLIC = os.getenv("BUCKET_KEY_PUBLIC")
    SRC_SFTP_HOST = os.getenv("SRC_SFTP_HOST")
    SRC_SFTP_USER = os.getenv("SRC_SFTP_USER")
    SRC_SFTP_PORT = os.getenv("SRC_SFTP_PORT")
    SRC_SFTP_PASS = os.getenv("SRC_SFTP_PASS")
    BUCKET_URL = "sfo3.digitaloceanspaces.com"

    config_text = f"""
[dst]
type = s3
provider = DigitalOcean
access_key_id = {BUCKET_KEY_PUBLIC}
secret_access_key = {BUCKET_KEY_SECRET}
endpoint = {BUCKET_URL}
bucket = {BUCKET_NAME}

[src]
type = sftp
host = {SRC_SFTP_HOST}
user = {SRC_SFTP_USER}
port = {SRC_SFTP_PORT}
pass = {SRC_SFTP_PASS}

"""
    return Config(config_text)


@pytest.mark.cloud
class RcloneCopyBytesTester(unittest.TestCase):
    """Test rclone functionality."""

    def setUp(self) -> None:
        """Check if all required environment variables are set before running tests."""
        skip_if_missing_cloud_env(self, DIGITAL_OCEAN_SPACES_ENV_VARS)
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    @unittest.skip("Skip for now - long running test")
    def test_copy_bytes_to_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir) / "tmp.mp4"
            rclone = Rclone(_generate_rclone_config())
            rclone.copy_bytes(
                src="dst:rclone-kit-unit-test/zachs_video/breaking_ai_mind.mp4",
                offset=0,
                length=1024 * 1024,
                outfile=tmp,
            )
            self.assertTrue(tmp.exists())
            tmp_size = tmp.stat().st_size
            self.assertEqual(tmp_size, 1024 * 1024)
        print("done")


if __name__ == "__main__":
    unittest.main()
