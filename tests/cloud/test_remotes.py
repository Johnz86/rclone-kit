"""
Unit test file.
"""

import os
import unittest

import pytest

from rclone_kit import Rclone, Remote
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneRemoteTests(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_cloud_rclone(self, cloud_rclone: Rclone) -> None:
        self.rclone = cloud_rclone

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_list_remotes(self) -> None:
        rclone = self.rclone

        remotes: list[Remote] = rclone.listremotes()
        self.assertGreater(len(remotes), 0)
        for remote in remotes:
            self.assertIsInstance(remote, Remote)
            print(remote)
        print("done")


if __name__ == "__main__":
    unittest.main()
