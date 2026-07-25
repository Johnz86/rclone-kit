import os
import unittest
from datetime import datetime

import pytest

from helpers import CLOUD_TEST_REMOTE_ROOT
from rclone_kit import Rclone
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneReadWriteText(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_cloud_rclone(self, cloud_rclone: Rclone) -> None:
        self.rclone = cloud_rclone

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_read_write(self) -> None:
        dst_dir = f"{CLOUD_TEST_REMOTE_ROOT}/test_data/read_write_test"

        rclone = self.rclone
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


if __name__ == "__main__":
    unittest.main()
