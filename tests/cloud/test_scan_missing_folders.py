"""
Unit test file.
"""

import os
import unittest

import pytest

from rclone_kit import Config, Dir, Rclone
from rclone_kit.env_file import load_env_file
from rclone_kit.types import Order

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneScanMissingFoldersTests(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_do_spaces_config(self, do_spaces_config: Config) -> None:
        self.config = do_spaces_config

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    @unittest.skip(
        "Manual test: exercises rclone.scan_missing_folders() end-to-end against a "
        "live bucket. Its diff logic (the part that used to be buggy - see "
        "tests/unit/test_scan_missing_folders_diff_logic.py) is now covered by a "
        "deterministic fake; this test only remains for real end-to-end "
        "verification. Also only ever compares src to itself here, so it can't "
        "exercise a real diff even when enabled - worth rewriting to use two "
        "distinct paths under the same bucket before relying on it."
    )
    def test_scan_missing_folders(self) -> None:
        """Test copying a single file to remote storage."""
        rclone = Rclone(self.config)
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
