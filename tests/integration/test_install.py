"""
Integration test for the rclone executable installer.
"""

import subprocess
import unittest

from rclone_kit.runtime.rclone_binary import resolve_rclone_executable


class RcloneInstallTester(unittest.TestCase):
    """Test that the resolver can install and run a verified rclone build."""

    def test_resolve_and_run_rclone(self) -> None:
        rclone_exe = resolve_rclone_executable(allow_path_lookup=True, allow_verified_download=True)
        self.assertTrue(rclone_exe.is_file())

        completed = subprocess.run(
            [str(rclone_exe), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
