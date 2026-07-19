"""
Unit test file.
"""

import unittest
from dataclasses import FrozenInstanceError, dataclass

import pytest

from rclone_kit import PartInfo, Range, SizeSuffix


@dataclass(frozen=True)
class InvalidRangeCase:
    start: int
    end: int


NEGATIVE_RANGE_START = InvalidRangeCase(start=-1, end=1)
EMPTY_RANGE = InvalidRangeCase(start=1, end=1)
REVERSED_RANGE = InvalidRangeCase(start=2, end=1)
INVALID_RANGE_CASES = [NEGATIVE_RANGE_START, EMPTY_RANGE, REVERSED_RANGE]
INVALID_RANGE_IDS = ["negative_start", "empty", "reversed"]


class RcloneSuffixSize(unittest.TestCase):
    """Test rclone functionality."""

    def test_simple_suffix(self) -> None:
        size_suffix = SizeSuffix(1024)
        size_suffix = SizeSuffix("16MB")
        size_int = size_suffix.as_int()
        self.assertEqual(size_int, 16 * 1024 * 1024)

    def test_float_suffix(self) -> None:
        size_suffix = SizeSuffix("16.5M")
        size_int = size_suffix.as_int()
        self.assertEqual(size_int, int(16.5 * 1024 * 1024))
        # now assert that the string value is the same as the input
        out_str = str(size_suffix)
        self.assertEqual(out_str, "16.5M")

    def test_float_suffix_border(self) -> None:
        size_suffix = SizeSuffix("1M")
        size_int = size_suffix.as_int()
        size_int -= 1
        # now assert that the string value is the same as the input
        tmp = SizeSuffix(size_int)
        out_str = tmp.as_str()
        self.assertEqual(out_str, "1M")


def test_less_than_or_equal_accepts_equal_value() -> None:
    assert SizeSuffix(1024) <= SizeSuffix("1K")


def test_size_suffix_is_immutable() -> None:
    size = SizeSuffix(1)

    with pytest.raises(FrozenInstanceError):
        size._size = 2  # type: ignore[misc]


def test_in_place_addition_returns_a_new_value() -> None:
    original = SizeSuffix(1)
    result = original

    result += 1

    assert original == 1
    assert result == 2
    assert result is not original


@pytest.mark.parametrize(
    "case",
    INVALID_RANGE_CASES,
    ids=INVALID_RANGE_IDS,
)
def test_range_rejects_invalid_bounds(case: InvalidRangeCase) -> None:
    with pytest.raises(ValueError):
        Range(case.start, case.end)


def test_zero_byte_file_has_no_multipart_parts() -> None:
    assert PartInfo.split_parts(0, 1) == []


def test_multipart_part_count_never_exceeds_provider_limit() -> None:
    with pytest.warns(UserWarning):
        parts = PartInfo.split_parts(10_001, 1)

    assert len(parts) <= 10_000
    assert parts[-1].range.end == 10_001


if __name__ == "__main__":
    unittest.main()
