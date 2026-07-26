"""Unit tests for `rclone_kit.native.build_info`."""

import json

from rclone_kit.native.build_info import NativeBuildInfo, parse_build_info


def test_parse_build_info_decodes_full_payload() -> None:
    payload = json.dumps(
        {
            "abiVersion": 1,
            "rcloneVersion": "v1.75.0-DEV",
            "rcloneCommit": "abc123",
            "goVersion": "go1.26.5",
            "buildTags": ["cmount"],
            "target": "windows/amd64",
        }
    ).encode("utf-8")

    info = parse_build_info(payload)

    assert info == NativeBuildInfo(
        abi_version=1,
        rclone_version="v1.75.0-DEV",
        rclone_commit="abc123",
        go_version="go1.26.5",
        build_tags=("cmount",),
        target="windows/amd64",
    )


def test_parse_build_info_defaults_missing_optional_fields() -> None:
    payload = json.dumps({"abiVersion": 1}).encode("utf-8")

    info = parse_build_info(payload)

    assert info.abi_version == 1
    assert info.rclone_version == ""
    assert info.build_tags == ()


def test_parse_build_info_handles_null_build_tags() -> None:
    payload = json.dumps({"abiVersion": 1, "buildTags": None}).encode("utf-8")
    assert parse_build_info(payload).build_tags == ()


def test_parse_build_info_handles_empty_payload() -> None:
    info = parse_build_info(b"")
    assert info.abi_version == 0
