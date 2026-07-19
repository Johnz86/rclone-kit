"""
Unit test file.
"""

import os
import unittest

import pytest

from rclone_kit import Config, Parsed
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneConfigParseTester(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_do_spaces_config(self, do_spaces_config: Config) -> None:
        self.config = do_spaces_config

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_copyfile(self) -> None:
        """Test copying a single file to remote storage."""
        parsed: Parsed = self.config.parse()
        print(parsed)
        print()


if __name__ == "__main__":
    unittest.main()
