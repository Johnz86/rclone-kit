"""Unit tests for `rclone_kit.s3.multipart.merge_state`."""

from typing import cast

import pytest

from rclone_kit.client import Rclone
from rclone_kit.exceptions import MergeStateError
from rclone_kit.s3.multipart.merge_state import MergeState, MergeStateJson, Part


def _stub_rclone() -> Rclone:
    return object.__new__(Rclone)


def test_part_from_json_raises_merge_state_error_on_missing_field() -> None:
    with pytest.raises(MergeStateError):
        Part.from_json({"part_number": 1})


def test_part_from_json_round_trips() -> None:
    part = Part.from_json({"part_number": 1, "s3_key": "key"})
    assert part == Part(part_number=1, s3_key="key")


def test_merge_state_from_json_raises_merge_state_error_for_malformed_part() -> None:
    data = cast(
        MergeStateJson,
        {
            "merge_path": "merge",
            "bucket": "bucket",
            "dst_key": "dst",
            "upload_id": "upload-id",
            "finished": [],
            "all": [{"part_number": 1}],
        },
    )
    with pytest.raises(MergeStateError):
        MergeState.from_json(rclone=_stub_rclone(), data=data)


def test_merge_state_from_json_builds_merge_state() -> None:
    data: MergeStateJson = {
        "merge_path": "merge",
        "bucket": "bucket",
        "dst_key": "dst",
        "upload_id": "upload-id",
        "finished": [],
        "all": [{"part_number": 1, "s3_key": "key"}],
    }
    merge_state = MergeState.from_json(rclone=_stub_rclone(), data=data)
    assert merge_state.all_parts == [Part(part_number=1, s3_key="key")]
