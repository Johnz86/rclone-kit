"""Unit tests for `rclone_kit.detail.copy_file_parts_resumable`."""

import pytest

from rclone_kit.detail.copy_file_parts_resumable import copy_file_parts_resumable
from rclone_kit.rclone_impl import RcloneImpl


def _stub_rclone_impl() -> RcloneImpl:
    return object.__new__(RcloneImpl)


def test_copy_file_parts_resumable_skips_merge_when_upload_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merge_calls: list[str] = []

    def _fail_upload(**_kwargs: object) -> None:
        raise RuntimeError("upload failed")

    def _record_merge(**kwargs: object) -> None:
        merge_calls.append(str(kwargs["info_path"]))

    monkeypatch.setattr(
        "rclone_kit.s3.multipart.upload_parts_resumable.upload_parts_resumable", _fail_upload
    )
    monkeypatch.setattr(
        "rclone_kit.s3.multipart.upload_parts_server_side_merge.s3_server_side_multi_part_merge",
        _record_merge,
    )

    with pytest.raises(RuntimeError, match="upload failed"):
        copy_file_parts_resumable(
            self=_stub_rclone_impl(), src="remote:src", dst_dir="remote:dst-parts"
        )

    assert merge_calls == []


def test_copy_file_parts_resumable_strips_trailing_slash_before_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merge_calls: list[str] = []

    monkeypatch.setattr(
        "rclone_kit.s3.multipart.upload_parts_resumable.upload_parts_resumable",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "rclone_kit.s3.multipart.upload_parts_server_side_merge.s3_server_side_multi_part_merge",
        lambda **kwargs: merge_calls.append(str(kwargs["info_path"])),
    )

    copy_file_parts_resumable(
        self=_stub_rclone_impl(), src="remote:src", dst_dir="remote:dst-parts/", verbose=False
    )

    assert merge_calls == ["remote:dst-parts/info.json"]
