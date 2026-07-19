"""
Unit test file for testing rclone mount functionality.
"""

import os
import subprocess
import unittest
from pathlib import Path

import pytest

from rclone_kit import Config, Process, Rclone
from rclone_kit.env_file import load_env_file

load_env_file()


@pytest.mark.cloud
@pytest.mark.mount
class RcloneMountTests(unittest.TestCase):
    """Test rclone mount functionality."""

    @pytest.fixture(autouse=True)
    def _inject_do_spaces_config(self, do_spaces_config: Config) -> None:
        self.config = do_spaces_config

    def setUp(self) -> None:
        self.bucket_name = os.getenv("BUCKET_NAME")
        self.mount_point = Path("test_mount")

        parent = self.mount_point.parent
        if not parent.exists():
            parent.mkdir(parents=True)

        os.environ["RCLONE_KIT_VERBOSE"] = "1"
        self.rclone = Rclone(self.config)

    @unittest.skip(
        "Manual test: requires a real OS mount facility (FUSE on Linux, WinFsp on "
        "Windows) plus a live bucket; not covered by a deterministic fake. Run "
        "manually when validating mount behavior."
    )
    def test_mount(self) -> None:
        """Test mounting a remote bucket."""
        remote_path = f"dst:{self.bucket_name}"
        process: Process | None = None

        try:
            mount = self.rclone.mount(remote_path, self.mount_point)
            process = mount.process
            assert process is not None
            self.assertIsNone(
                process.poll(), "Mount process should still be running after 2 seconds"
            )

            self.assertTrue(self.mount_point.exists())
            self.assertTrue(self.mount_point.is_dir())

            contents = list(self.mount_point.iterdir())
            self.assertGreater(len(contents), 0, "Mounted directory should not be empty")

        except subprocess.CalledProcessError as e:
            self.fail(f"Mount operation failed: {e!s}")
        finally:
            if process:
                if process.poll() is None:
                    process.kill()
                stdout = process.stdout
                if stdout:
                    for line in stdout:
                        print(line)
                stderr = process.stderr
                if stderr:
                    for line in stderr:
                        print(line)
                process.kill()


if __name__ == "__main__":
    unittest.main()
