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


def test_from_json_returns_end_of_stream_for_none() -> None:
    assert isinstance(FinishedPiece.from_json(None), EndOfStream)


def test_from_json_parses_standard_field_names() -> None:
    result = FinishedPiece.from_json({"PartNumber": 3, "ETag": '"etag-3"'})

    assert result == FinishedPiece(part_number=3, etag="etag-3")


def test_from_json_parses_lowercase_field_names() -> None:
    result = FinishedPiece.from_json({"part_number": 4, "etag": "etag-4"})

    assert result == FinishedPiece(part_number=4, etag="etag-4")


def test_from_json_does_not_treat_a_part_number_of_zero_as_absent() -> None:
    # Regression test: `data.get("PartNumber") or data.get("part_number")`
    # used to fall through on any falsy value, not just a missing key, so a
    # PartNumber of 0 would be silently replaced by the fallback lookup.
    result = FinishedPiece.from_json({"PartNumber": 0, "ETag": "etag-0"})

    assert result == FinishedPiece(part_number=0, etag="etag-0")
