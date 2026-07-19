import os
import unittest
from pathlib import Path

import pytest

from helpers import CLOUD_TEST_REMOTE_ROOT, DIGITAL_OCEAN_SPACES_ENV_VARS, skip_if_missing_cloud_env
from rclone_kit import PartInfo, Rclone, SizeSuffix
from rclone_kit.env_file import load_env_file

_HERE = Path(__file__).parent
_PROJECT_ROOT = _HERE.parent
_CONFIG_PATH = _PROJECT_ROOT / "rclone-mounted-ranged-download.conf"

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


def _generate_rclone_config(port: int) -> str:
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


[webdav]
type = webdav
user = guest
# obscured password for "1234", use Rclone.obscure("1234") to generate
pass = d4IbQLV9W0JhI2tm5Zp88hpMtEg
url = http://localhost:{port}
vendor = rclone
"""

    return config_text


PORT = 8095


@pytest.mark.cloud
class RcloneCopyResumableFileToS3(unittest.TestCase):
    """Test rclone functionality."""

    def setUp(self) -> None:
        """Check if all required environment variables are set before running tests."""
        skip_if_missing_cloud_env(self, DIGITAL_OCEAN_SPACES_ENV_VARS)
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    @unittest.skip("takes a long time to run")
    def test_copy_parts(self) -> None:
        src_file = f"{CLOUD_TEST_REMOTE_ROOT}/zachs_video/global_alliance.mp4"
        dst = f"{CLOUD_TEST_REMOTE_ROOT}/test_data/global_alliance.mp4"
        dst_dir = f"{CLOUD_TEST_REMOTE_ROOT}/test_data/global_alliance.mp4-parts"

        config_text = _generate_rclone_config(PORT)
        _CONFIG_PATH.write_text(config_text, encoding="utf-8")
        rclone = Rclone(_CONFIG_PATH)

        try:
            src_size: SizeSuffix | Exception = rclone.impl.size_file(src_file)
            assert isinstance(src_size, SizeSuffix)

            part_infos: list[PartInfo] = PartInfo.split_parts(
                size=src_size, target_chunk_size=src_size / 2
            )

            err = rclone.copy_file_s3_resumable(
                src=src_file,
                dst=dst,
                part_infos=part_infos,
            )
            assert not isinstance(err, Exception)

            rclone.copy_file_s3_resumable(
                src=src_file,
                dst=dst,
                part_infos=part_infos,
            )

            dir_listing = rclone.ls(dst)
            self.assertEqual(len(dir_listing.files), 1)
            expected_files = dir_listing.files[0]
            self.assertEqual(expected_files.name, "global_alliance.mp4")
            self.assertEqual(expected_files.size, src_size)
        finally:
            rclone.purge(dst_dir)
            _CONFIG_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
