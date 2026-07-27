"""Unit tests for the embedded RC-backed `operations/check` verification
and its typed `CheckResult` report.

Uses a fake `RcClient`-shaped object driven by one canned response, the
same pattern `tests/unit/test_listing_ops_embedded.py` establishes, so
these tests exercise request/response mapping without a built native
library.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from rclone_kit.check import CheckResult
from rclone_kit.config import Config
from rclone_kit.diff import DiffType
from rclone_kit.operations.check_ops_embedded import (
    CHECK_METHOD,
    parse_check_result,
    run_check_embedded,
)

_SRC = "src:bucket"
_DST = "dst:bucket"

_S3_CONFIG_TEXT = """
[do-remote]
type = s3
provider = DigitalOcean
access_key_id = AKIAEXAMPLE
secret_access_key = super-secret
"""

_OK_RESPONSE: dict[str, object] = {"success": True, "status": "OK", "hashType": "md5"}

_DIFFERENCES_RESPONSE: dict[str, object] = {
    "success": False,
    "status": "1 differences found",
    "hashType": "md5",
    "combined": [
        f"{DiffType.EQUAL.value} same.txt",
        f"{DiffType.DIFFERENT.value} differs.txt",
        f"{DiffType.MISSING_ON_SRC.value} only-on-dst.txt",
        f"{DiffType.MISSING_ON_DST.value} only-on-src.txt",
        f"{DiffType.ERROR.value} unreadable.txt",
    ],
    "missingOnSrc": ["only-on-dst.txt"],
    "missingOnDst": ["only-on-src.txt"],
    "match": ["same.txt"],
    "differ": ["differs.txt"],
    "error": ["unreadable.txt"],
}

_VALID_CHECK_RESULT_FIELDS: dict[str, object] = {"success": True, "status": "OK"}


class FakeRcClient:
    """A fake `RcClient` returning one canned response for every call."""

    def __init__(self, response: dict[str, object]) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.response = response

    def call(self, method: str, **params: object) -> dict:
        self.calls.append((method, params))
        return dict(self.response)


def _empty_config() -> Config:
    return Config("")


@dataclass(frozen=True)
class InvalidCheckResultCase:
    fields: dict[str, object]
    expected_message: str


BARE_STRING_REPORT_ARRAY = InvalidCheckResultCase({"differ": "a.txt"}, "bare string")
NON_ITERABLE_REPORT_ARRAY = InvalidCheckResultCase({"differ": 3}, "iterable of strings")
NON_STRING_REPORT_ENTRY = InvalidCheckResultCase({"differ": [1]}, "only strings")
EMPTY_STATUS = InvalidCheckResultCase({"status": ""}, "non-empty summary string")
EMPTY_HASH_TYPE = InvalidCheckResultCase({"hash_type": ""}, "non-empty hash name")
NON_BOOL_SUCCESS = InvalidCheckResultCase({"success": 1}, "must be a bool")
FAILURE_CARRYING_SUCCESS_STATUS = InvalidCheckResultCase({"success": False}, "success status")

INVALID_CHECK_RESULT_CASES = [
    BARE_STRING_REPORT_ARRAY,
    NON_ITERABLE_REPORT_ARRAY,
    NON_STRING_REPORT_ENTRY,
    EMPTY_STATUS,
    EMPTY_HASH_TYPE,
    NON_BOOL_SUCCESS,
    FAILURE_CARRYING_SUCCESS_STATUS,
]


@pytest.mark.parametrize(
    "case",
    INVALID_CHECK_RESULT_CASES,
    ids=[
        "bare_string_report_array",
        "non_iterable_report_array",
        "non_string_report_entry",
        "empty_status",
        "empty_hash_type",
        "non_bool_success",
        "failure_carrying_success_status",
    ],
)
def test_check_result_rejects_an_invalid_report(case: InvalidCheckResultCase) -> None:
    with pytest.raises(ValueError, match=case.expected_message):
        CheckResult(**{**_VALID_CHECK_RESULT_FIELDS, **case.fields})  # type: ignore[arg-type]


def test_check_result_freezes_report_arrays_into_tuples() -> None:
    result = CheckResult(success=True, status="OK", differ=["a.txt", "b.txt"])  # type: ignore[arg-type]

    assert result.differ == ("a.txt", "b.txt")


def test_run_check_sends_only_the_two_sides_and_the_always_explicit_flags() -> None:
    rc_client = FakeRcClient(_OK_RESPONSE)

    run_check_embedded(rc_client, _empty_config(), _SRC, _DST)

    method, params = rc_client.calls[0]
    assert method == CHECK_METHOD
    assert params == {
        "srcFs": _SRC,
        "dstFs": _DST,
        "oneWay": False,
        "download": False,
    }


def test_run_check_forwards_every_explicitly_requested_report_array() -> None:
    rc_client = FakeRcClient(_OK_RESPONSE)

    run_check_embedded(
        rc_client,
        _empty_config(),
        _SRC,
        _DST,
        one_way=True,
        download=True,
        combined=True,
        missing_on_src=False,
        missing_on_dst=False,
        match=True,
        differ=True,
        error=False,
    )

    _method, params = rc_client.calls[0]
    assert params == {
        "srcFs": _SRC,
        "dstFs": _DST,
        "oneWay": True,
        "download": True,
        "combined": True,
        "missingOnSrc": False,
        "missingOnDst": False,
        "match": True,
        "differ": True,
        "error": False,
    }


def test_run_check_encodes_tuning_into_the_config_overlay() -> None:
    rc_client = FakeRcClient(_OK_RESPONSE)

    run_check_embedded(
        rc_client, _empty_config(), _SRC, _DST, checkers=64, size_only=True, fast_list=True
    )

    _method, params = rc_client.calls[0]
    assert params["_config"] == {"Checkers": 64, "SizeOnly": True, "UseListR": True}


def test_run_check_rejects_a_checkers_value_rclone_cannot_act_on() -> None:
    rc_client = FakeRcClient(_OK_RESPONSE)

    with pytest.raises(ValueError, match="checkers must be a positive integer"):
        run_check_embedded(rc_client, _empty_config(), _SRC, _DST, checkers=0)

    assert rc_client.calls == []


def test_run_check_encodes_an_s3_source_with_no_check_bucket() -> None:
    rc_client = FakeRcClient(_OK_RESPONSE)

    run_check_embedded(rc_client, Config(_S3_CONFIG_TEXT), "do-remote:bucket", _DST)

    _method, params = rc_client.calls[0]
    assert params["srcFs"] == {
        "_name": "do-remote",
        "_root": "bucket",
        "no_check_bucket": "true",
    }


def test_run_check_returns_a_successful_report_with_no_arrays_requested() -> None:
    rc_client = FakeRcClient(_OK_RESPONSE)

    result = run_check_embedded(rc_client, _empty_config(), _SRC, _DST)

    assert result == CheckResult(success=True, status="OK", hash_type="md5")


def test_run_check_returns_a_failure_report_instead_of_raising() -> None:
    rc_client = FakeRcClient(_DIFFERENCES_RESPONSE)

    result = run_check_embedded(rc_client, _empty_config(), _SRC, _DST)

    assert result.success is False
    assert result.status == "1 differences found"
    assert result.differ == ("differs.txt",)


def test_parse_check_result_maps_every_report_array_to_its_field() -> None:
    result = parse_check_result(_DIFFERENCES_RESPONSE)

    assert result.hash_type == "md5"
    assert result.combined == tuple(_DIFFERENCES_RESPONSE["combined"])  # type: ignore[arg-type]
    assert result.missing_on_src == ("only-on-dst.txt",)
    assert result.missing_on_dst == ("only-on-src.txt",)
    assert result.match == ("same.txt",)
    assert result.differ == ("differs.txt",)
    assert result.error == ("unreadable.txt",)


def test_parse_check_result_treats_omitted_arrays_as_empty() -> None:
    result = parse_check_result({"success": True, "status": "OK"})

    assert result.hash_type is None
    assert (
        result.combined
        == result.missing_on_src
        == result.missing_on_dst
        == result.match
        == result.differ
        == result.error
        == ()
    )


def test_parse_check_result_rejects_a_report_array_that_is_not_an_array() -> None:
    with pytest.raises(ValueError, match="must be an array of strings"):
        parse_check_result({"success": True, "status": "OK", "differ": "a.txt"})


def test_parse_check_result_rejects_a_response_with_no_status() -> None:
    with pytest.raises(ValueError, match="non-empty summary string"):
        parse_check_result({"success": True})
