"""Unit tests for `rclone_kit.operations.s3_ops`.

Covers the two behaviors that are the module's own rather than boto3's:
refusing a non-S3 destination before any credential lookup, and deriving
the `<dst>-parts` staging directory the resumable upload merges from.
"""

from pathlib import Path

import pytest

from rclone_kit.client import Rclone
from rclone_kit.operations.s3_ops import (
    _PARTS_DIR_SUFFIX,
    copy_file_parts_s3_resumable,
    upload_file_s3,
)
from rclone_kit.s3.types import S3Credentials

_DST = "remote:bucket/object.bin"
_SRC = "remote:bucket/source.bin"


class FakeS3UploadAccess:
    """An `S3UploadAccess` that records credential lookups and refuses to
    answer one: no test here is allowed to reach the boto3 boundary.
    """

    def __init__(self, *, is_s3: bool) -> None:
        self._is_s3 = is_s3
        self.is_s3_queries: list[str] = []
        self.credential_lookups: list[tuple[str, bool | None]] = []

    def is_s3(self, dst: str) -> bool:
        self.is_s3_queries.append(dst)
        return self._is_s3

    def get_s3_credentials(self, remote: str, verbose: bool | None = None) -> S3Credentials:
        self.credential_lookups.append((remote, verbose))
        raise AssertionError("no test here reaches a real credential lookup")


def test_upload_file_rejects_a_non_s3_destination_before_looking_up_credentials(
    tmp_path: Path,
) -> None:
    access = FakeS3UploadAccess(is_s3=False)

    with pytest.raises(ValueError, match="not an S3 remote"):
        upload_file_s3(access, tmp_path / "payload.bin", _DST)

    assert access.credential_lookups == []


@pytest.mark.parametrize(
    "dst",
    [_DST, f"{_DST}/"],
    ids=["bare_object_path", "trailing_slash"],
)
def test_copy_file_parts_resumable_stages_parts_in_a_sibling_directory(
    dst: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trailing slash must be normalized away: it would otherwise stage
    the parts under `<dst>/-parts`, where the merge step never looks for
    `info.json`.
    """
    staged: list[str] = []

    def record_parts_dir(**kwargs: object) -> None:
        staged.append(str(kwargs["dst_dir"]))

    monkeypatch.setattr("rclone_kit.operations.s3_ops.copy_file_parts_resumable", record_parts_dir)

    copy_file_parts_s3_resumable(object.__new__(Rclone), _SRC, dst)

    assert staged == [f"{_DST}{_PARTS_DIR_SUFFIX}"]
