"""
Unit test file.
"""

import os
import tempfile
import unittest
from pathlib import Path

import pytest

from rclone_kit import Rclone
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneCopyBytesTester(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_cloud_rclone(self, cloud_rclone: Rclone) -> None:
        self.rclone = cloud_rclone

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    @unittest.skip(
        "Manual test: downloads a real 1MB range from a live bucket; long-running "
        "and not covered by a deterministic fake."
    )
    def test_copy_bytes_to_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir) / "tmp.mp4"
            rclone = self.rclone
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
