"""Live test file.

Round-trips the small-payload read/write helpers against a scoped test
prefix, and reads a byte range directly to a local file.
"""

from pathlib import Path

import pytest

from rclone_kit import Rclone, SizeSuffix

pytestmark = pytest.mark.live_s3


def test_write_text_then_read_text_round_trips(live_rclone: Rclone, live_test_prefix: str) -> None:
    path = f"{live_test_prefix}/note.txt"

    live_rclone.write_text("hello from the live suite", path)

    assert live_rclone.read_text(path) == "hello from the live suite"


def test_write_bytes_then_read_bytes_round_trips(
    live_rclone: Rclone, live_test_prefix: str
) -> None:
    path = f"{live_test_prefix}/blob.bin"
    payload = bytes(range(256))

    live_rclone.write_bytes(payload, path)

    assert live_rclone.read_bytes(path) == payload


def test_copy_bytes_reads_a_slice_of_a_remote_object(
    live_rclone: Rclone, live_test_prefix: str, tmp_path: Path
) -> None:
    path = f"{live_test_prefix}/range.bin"
    payload = bytes(range(256)) * 4
    live_rclone.write_bytes(payload, path)
    outfile = tmp_path / "slice.bin"

    live_rclone.copy_bytes(
        src=path,
        offset=SizeSuffix(10),
        length=SizeSuffix(20),
        outfile=outfile,
    )

    assert outfile.read_bytes() == payload[10:30]
