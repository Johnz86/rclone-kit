"""Unit tests for `rclone_kit.s3.multipart.info_json`'s part-name parsing.

Regression coverage for a review finding: `fetch_all_finished_part_numbers`
used to parse the part number back out of a file name via ad hoc string
splitting (`name.split("_")[0].split(".")[1]`), tightly coupled to
`upload_parts_resumable._gen_name`'s exact format with no shared constant
or parse function between generation and parsing - a mismatch would raise
an opaque `IndexError`/`ValueError` instead of a clear one.
"""

import pytest

from rclone_kit.s3.multipart.info_json import _parse_part_number
from rclone_kit.s3.multipart.upload_parts_resumable import _gen_name
from rclone_kit.types import SizeSuffix


def test_parse_part_number_round_trips_through_gen_name() -> None:
    name = _gen_name(7, SizeSuffix(0), SizeSuffix(1024))

    assert _parse_part_number(name) == 7


def test_parse_part_number_rejects_an_unrecognized_name_with_a_clear_error() -> None:
    with pytest.raises(ValueError, match="does not match the expected"):
        _parse_part_number("not-a-part-name.txt")
