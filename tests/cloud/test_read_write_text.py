import os
import unittest
from datetime import datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

from helpers import CLOUD_TEST_REMOTE_ROOT, DIGITAL_OCEAN_SPACES_ENV_VARS, skip_if_missing_cloud_env
from rclone_kit import Rclone

_HERE = Path(__file__).parent
_PROJECT_ROOT = _HERE.parent
_CONFIG_PATH = _PROJECT_ROOT / "rclone-mounted-ranged-download.conf"

load_dotenv()

BUCKET_NAME = os.getenv("BUCKET_NAME")


def _generate_rclone_config() -> str:
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
    # _CONFIG_PATH.write_text(config_text, encoding="utf-8")
    # print(f"Config file written to: {_CONFIG_PATH}")
    return config_text


@pytest.mark.cloud
class RcloneReadWriteText(unittest.TestCase):
    """Test rclone functionality."""

    def setUp(self) -> None:
        """Check if all required environment variables are set before running tests."""
        skip_if_missing_cloud_env(self, DIGITAL_OCEAN_SPACES_ENV_VARS)
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_read_write(self) -> None:
        dst_dir = f"{CLOUD_TEST_REMOTE_ROOT}/test_data/read_write_test"

        config_text = _generate_rclone_config()
        _CONFIG_PATH.write_text(config_text, encoding="utf-8")
        rclone = Rclone(_CONFIG_PATH)
        dst_file = f"{dst_dir}/hello.txt"
        try:
            rclone.write_text(
                text="Hello, World!",
                dst=dst_file,
            )

            out = rclone.read_text(dst_file)
            self.assertEqual("Hello, World!", out)
            mod_time_dt = rclone.modtime_dt(dst_file)
            assert isinstance(mod_time_dt, datetime)

            dir_listing = rclone.ls(dst_dir)
            self.assertIsNotNone(dir_listing)
        finally:
            rclone.purge(dst_dir)
            _CONFIG_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
