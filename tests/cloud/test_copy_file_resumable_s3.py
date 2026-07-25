import os
import unittest

import pytest

from helpers import CLOUD_TEST_REMOTE_ROOT
from rclone_kit import PartInfo, Rclone, SizeSuffix
from rclone_kit.env_file import load_env_file

load_env_file()

BUCKET_NAME = os.getenv("BUCKET_NAME")


@pytest.mark.cloud
class RcloneCopyResumableFileToS3(unittest.TestCase):
    """Test rclone functionality."""

    @pytest.fixture(autouse=True)
    def _inject_cloud_rclone(self, cloud_rclone: Rclone) -> None:
        self.rclone = cloud_rclone

    def setUp(self) -> None:
        os.environ["RCLONE_KIT_VERBOSE"] = "1"

    @unittest.skip(
        "Manual test: performs a real resumable multi-part S3 copy against a live "
        "bucket; long-running and not covered by a deterministic fake."
    )
    def test_copy_parts(self) -> None:
        src_file = f"{CLOUD_TEST_REMOTE_ROOT}/zachs_video/global_alliance.mp4"
        dst = f"{CLOUD_TEST_REMOTE_ROOT}/test_data/global_alliance.mp4"
        dst_dir = f"{CLOUD_TEST_REMOTE_ROOT}/test_data/global_alliance.mp4-parts"

        rclone = self.rclone

        try:
            src_size: SizeSuffix = rclone.size_file(src_file)

            part_infos: list[PartInfo] = PartInfo.split_parts(
                size=src_size, target_chunk_size=src_size / 2
            )

            rclone.copy_file_s3_resumable(
                src=src_file,
                dst=dst,
                part_infos=part_infos,
            )

            rclone.copy_file_s3_resumable(
                src=src_file,
                dst=dst,
                part_infos=part_infos,
            )

            dir_listing = rclone.ls(dst)
            self.assertEqual(len(dir_listing.files), 1)
            expected_files = dir_listing.files[0]
            self.assertEqual(expected_files.name, "global_alliance.mp4")
            self.assertEqual(expected_files.size, src_size)
        finally:
            rclone.purge(dst_dir)


if __name__ == "__main__":
    unittest.main()
