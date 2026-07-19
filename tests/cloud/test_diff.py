"""
Unit test file.
"""

import os
import unittest

import pytest

from helpers import CLOUD_TEST_REMOTE_ROOT, DIGITAL_OCEAN_SPACES_ENV_VARS, skip_if_missing_cloud_env
from rclone_kit import Config, Rclone
from rclone_kit.diff import DiffItem, DiffOption, DiffType
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


def _generate_rclone_config() -> Config:
    BUCKET_KEY_SECRET = os.getenv("BUCKET_KEY_SECRET")
    BUCKET_KEY_PUBLIC = os.getenv("BUCKET_KEY_PUBLIC")
    BUCKET_URL = "sfo3.digitaloceanspaces.com"

    config_text = f"""
[dst]
type = s3
provider = DigitalOcean
access_key_id = {BUCKET_KEY_PUBLIC}
secret_access_key = {BUCKET_KEY_SECRET}
endpoint = {BUCKET_URL}
"""

    out = Config(config_text)
    return out


@pytest.mark.cloud
class RcloneDiffTests(unittest.TestCase):
    """Test rclone functionality."""

    def setUp(self) -> None:
        """Check if all required environment variables are set before running tests."""
        skip_if_missing_cloud_env(self, DIGITAL_OCEAN_SPACES_ENV_VARS)
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    def test_diff(self) -> None:
        """Test copying a single file to remote storage."""
        rclone = Rclone(_generate_rclone_config())
        item: DiffItem
        all: list[DiffItem] = []
        for item in rclone.diff(CLOUD_TEST_REMOTE_ROOT, CLOUD_TEST_REMOTE_ROOT):
            self.assertEqual(item.type, DiffType.EQUAL)  # should be equal because same repo
            all.append(item)
        self.assertGreater(len(all), 10)
        msg = "\n".join([str(item) for item in all])
        print(msg)

    def test_min_max_size(self) -> None:
        rclone = Rclone(_generate_rclone_config())
        item: DiffItem
        all: list[DiffItem] = list(
            rclone.diff(CLOUD_TEST_REMOTE_ROOT, CLOUD_TEST_REMOTE_ROOT, min_size="70M")
        )
        for item in all:
            if "internaly_ai_alignment.mp4" in item.path:
                break
        else:
            self.fail("internaly_ai_alignment.mp4 not found")
        all.clear()
        all = list(rclone.diff(CLOUD_TEST_REMOTE_ROOT, CLOUD_TEST_REMOTE_ROOT, max_size="70M"))
        for item in all:
            if "internaly_ai_alignment.mp4" in item.path:
                self.fail("internaly_ai_alignment.mp4 not filtered")

    def test_diff_missing_on_dst(self) -> None:
        rclone = Rclone(_generate_rclone_config())
        item: DiffItem
        all: list[DiffItem] = []
        for item in rclone.diff(
            CLOUD_TEST_REMOTE_ROOT,
            f"{CLOUD_TEST_REMOTE_ROOT}/does-not-exist",
            diff_option=DiffOption.MISSING_ON_DST,
        ):
            self.assertEqual(
                item.type, DiffType.MISSING_ON_DST
            )  # should be equal because same repo
            all.append(item)
        self.assertGreaterEqual(len(all), 46)
        msg = "\n".join([str(item) for item in all])
        print(msg)

    def test_diff_missing_on_src(self) -> None:
        rclone = Rclone(_generate_rclone_config())
        item: DiffItem
        all: list[DiffItem] = []
        for item in rclone.diff(
            f"{CLOUD_TEST_REMOTE_ROOT}/does-not-exist",
            CLOUD_TEST_REMOTE_ROOT,
            diff_option=DiffOption.MISSING_ON_SRC,
        ):
            self.assertEqual(item.type, DiffType.MISSING_ON_SRC)
            all.append(item)
        self.assertGreaterEqual(len(all), 46)
        msg = "\n".join([str(item) for item in all])
        print(msg)


if __name__ == "__main__":
    unittest.main()
