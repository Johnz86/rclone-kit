"""Live test file.

Exercises the S3-optimized upload paths directly against the Ceph
endpoint: a single-file direct upload and a resumable, chunked multipart
upload. Requires the `s3` extra (`boto3`); skipped if it is not installed.

`live_test_prefix`'s teardown purges the whole scoped prefix recursively,
which also covers the `-parts` staging path `copy_file_s3_resumable`
creates beside its destination.
"""

from pathlib import Path

import pytest

from rclone_kit import PartInfo, Rclone, SizeSuffix

pytestmark = pytest.mark.live_s3

pytest.importorskip("boto3")

_RESUMABLE_PAYLOAD_SIZE = 13 * 1024 * 1024


def test_copy_file_s3_uploads_a_local_file_directly(
    live_rclone: Rclone, live_test_prefix: str, tmp_path: Path
) -> None:
    local_file = tmp_path / "direct.bin"
    local_file.write_bytes(b"direct upload payload")
    dst = f"{live_test_prefix}/direct.bin"

    live_rclone.copy_file_s3(src=local_file, dst=dst)

    assert live_rclone.read_bytes(dst) == b"direct upload payload"


def test_get_s3_credentials_resolves_the_configured_remote(
    live_rclone: Rclone, live_test_prefix: str, live_test_bucket: str
) -> None:
    credentials = live_rclone.get_s3_credentials(live_test_prefix)

    assert credentials.access_key_id
    assert credentials.secret_access_key
    assert credentials.bucket_name == live_test_bucket


def test_copy_file_s3_resumable_uploads_a_multipart_file(
    live_rclone: Rclone, live_test_prefix: str
) -> None:
    src = f"{live_test_prefix}/source.bin"
    dst = f"{live_test_prefix}/resumable.bin"
    payload = (b"0123456789abcdef" * (_RESUMABLE_PAYLOAD_SIZE // 16))[:_RESUMABLE_PAYLOAD_SIZE]
    live_rclone.write_bytes(payload, src)

    part_infos = PartInfo.split_parts(
        size=SizeSuffix(len(payload)),
        target_chunk_size=SizeSuffix("6M"),
    )
    assert len(part_infos) > 1

    live_rclone.copy_file_s3_resumable(src=src, dst=dst, part_infos=part_infos)

    assert live_rclone.read_bytes(dst) == payload
