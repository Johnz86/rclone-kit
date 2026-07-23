"""Typed decoding of `RcloneKitBuildInfo`/`RcloneKitInitialize` response JSON."""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class NativeBuildInfo:
    """One decoded `RcloneKitBuildInfo`/`RcloneKitInitialize` response.

    See `native/rclone/librclone/rclonekit/abi.h` for the field contract.
    """

    abi_version: int
    rclone_version: str
    rclone_commit: str
    go_version: str
    build_tags: tuple[str, ...]
    target: str


def parse_build_info(payload: bytes) -> NativeBuildInfo:
    """Decode a `RcloneKitBuildInfo`/`RcloneKitInitialize` JSON payload.

    Missing optional string fields decode as `""`; a missing/null
    `buildTags` decodes as an empty tuple.
    """
    data = json.loads(payload.decode("utf-8")) if payload else {}
    return NativeBuildInfo(
        abi_version=data.get("abiVersion", 0),
        rclone_version=data.get("rcloneVersion", ""),
        rclone_commit=data.get("rcloneCommit", ""),
        go_version=data.get("goVersion", ""),
        build_tags=tuple(data.get("buildTags") or ()),
        target=data.get("target", ""),
    )
