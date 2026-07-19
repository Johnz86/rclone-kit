"""Unit tests for `rclone_kit.s3.multipart.finished_piece.FinishedPiece`."""

import pytest

from rclone_kit.s3.multipart.finished_piece import FinishedPiece
from rclone_kit.types import EndOfStream


def test_to_json_array_excludes_end_of_stream_and_sorts_by_part_number() -> None:
    parts = [
        FinishedPiece(part_number=2, etag="etag-2"),
        EndOfStream(),
        FinishedPiece(part_number=1, etag="etag-1"),
    ]

    result = FinishedPiece.to_json_array(parts)

    assert result == [
        {"PartNumber": 1, "ETag": "etag-1"},
        {"PartNumber": 2, "ETag": "etag-2"},
    ]


def test_to_json_array_warns_when_more_than_one_end_of_stream_present() -> None:
    parts = [
        FinishedPiece(part_number=1, etag="etag-1"),
        EndOfStream(),
        EndOfStream(),
    ]

    with pytest.warns(UserWarning, match="Only one EndOfStream should be present"):
        FinishedPiece.to_json_array(parts)


def test_to_json_array_does_not_warn_with_a_single_end_of_stream(
    recwarn: pytest.WarningsRecorder,
) -> None:
    parts = [FinishedPiece(part_number=1, etag="etag-1"), EndOfStream()]

    FinishedPiece.to_json_array(parts)

    assert len(recwarn) == 0
