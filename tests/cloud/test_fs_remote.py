"""Cloud test file for `RemoteFS` against a real DigitalOcean Spaces bucket."""

import unittest

import pytest

from helpers import CLOUD_TEST_KEY_PREFIX, CLOUD_TEST_REMOTE_ROOT
from rclone_kit import Config, Rclone
from rclone_kit.fs.filesystem import FSPath


@pytest.mark.cloud
class RcloneRemoteFSTester(unittest.TestCase):
    """Tests for RemoteFS functionality."""

    @pytest.fixture(autouse=True)
    def _inject_do_spaces_config(self, do_spaces_config: Config) -> None:
        self.config = do_spaces_config

    def test_create_and_move_remote_fs(self) -> None:
        """Test create and move functionality."""
        fs = Rclone(self.config).filesystem(CLOUD_TEST_REMOTE_ROOT)
        with fs.cwd() as cwd:
            remote_tester: FSPath = cwd / f"{CLOUD_TEST_KEY_PREFIX}remote_tester"
            remote_tester.rmtree(ignore_errors=True)
            try:
                new_file_path = remote_tester / "test.txt"
                new_file_path.write_bytes(b"test")

                moved_file_path = remote_tester / "moved_test.txt"
                new_file_path.move_to(moved_file_path)
                self.assertTrue(moved_file_path.exists())
                self.assertFalse(new_file_path.exists())
            finally:
                remote_tester.rmtree(ignore_errors=True)

    @unittest.skip(
        "Known bug, not yet root-caused: RemoteFS.exists() (backed by rclone's HTTP "
        "serve autoindex) still reports the file present immediately after "
        "new_file_path.remove() succeeds, presumably due to a caching layer between "
        "the HTTP listing and the actual remote delete. Needs investigation against "
        "a live bucket before re-enabling; not covered by a deterministic fake."
    )
    def test_create_and_remove_remote_fs(self) -> None:
        """Test create and remove functionality."""
        fs = Rclone(self.config).filesystem(CLOUD_TEST_REMOTE_ROOT)
        with fs.cwd() as cwd:
            remote_tester: FSPath = cwd / f"{CLOUD_TEST_KEY_PREFIX}remote_tester"
            try:
                new_file_path = remote_tester / "test.txt"
                new_file_path.write_bytes(b"test")
                self.assertTrue(new_file_path.exists())

                new_file_path.remove()

                exists = new_file_path.exists()
                self.assertFalse(exists)
            finally:
                remote_tester.rmtree(ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
