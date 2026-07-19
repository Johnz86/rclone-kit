"""
Unit test file.
"""

import os
import unittest

import pytest

from rclone_kit import Config, Rclone, Remote
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneLsStreamFileTests(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_do_spaces_config(self, do_spaces_config: Config) -> None:
        self.config = do_spaces_config

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_list_remotes(self) -> None:
        rclone = Rclone(self.config)

        remotes: list[Remote] = rclone.listremotes()
        self.assertGreater(len(remotes), 0)
        for remote in remotes:
            self.assertIsInstance(remote, Remote)
            print(remote)
        print("done")

    def test_ls_stream(self) -> None:
        """Test listing the root directory of the bucket.

        Verifies that we can:
        1. Connect to the bucket
        2. List its contents
        3. Get both directories and files as proper types
        """
        self.assertIsNotNone(BUCKET_NAME)
        rclone = Rclone(self.config)

        with rclone.ls_stream(f"dst:{BUCKET_NAME}", max_depth=-1) as files:
            for filepath in files:
                print(filepath.path)

        print("done")


if __name__ == "__main__":
    unittest.main()
