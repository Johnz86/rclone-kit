"""
Unit test file.
"""

import os
import unittest

import pytest

from rclone_kit import Config, Rclone
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneRemoteControlTests(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_do_spaces_config(self, do_spaces_config: Config) -> None:
        self.config = do_spaces_config

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_server_launch(self) -> None:
        rclone = Rclone(self.config)
        proc = rclone.launch_server(addr="localhost:8888")
        try:
            self.assertTrue(proc.returncode is None)
        finally:
            proc.kill()

    def test_launch_server_and_control_it(self) -> None:
        rclone = Rclone(self.config)
        proc = rclone.launch_server(addr="localhost:8889")
        try:
            self.assertTrue(proc.returncode is None)

            cp = rclone.remote_control(addr="localhost:8889")

            print(cp.stdout)
            print("done")
        finally:
            proc.kill()


if __name__ == "__main__":
    unittest.main()
