"""Unit tests for the error-model migration of the S3 upload-side modules:
`file_part`, `s3.basic_ops`, `s3.multipart.upload_parts_resumable`, and
`s3.multipart.info_json`.
"""

from typing import Any, cast

import pytest

from rclone_kit.file_part import FilePart
from rclone_kit.s3.basic_ops import upload_file
from rclone_kit.s3.multipart.file_info import S3FileInfo
from rclone_kit.s3.multipart.info_json import InfoJson
from rclone_kit.s3.multipart.upload_parts_resumable import _check_part_size
from rclone_kit.types import PartInfo, SizeSuffix


def _s3_file_info() -> S3FileInfo:
    return S3FileInfo(upload_id="upload-id", part_number=1)


def test_file_part_get_file_raises_original_error() -> None:
    failure = OSError("fetch failed")
    part = FilePart(payload=failure, extra=_s3_file_info())

    with pytest.raises(OSError, match="fetch failed"):
        part.get_file()


def test_file_part_get_file_returns_payload_path(tmp_path) -> None:
    chunk = tmp_path / "chunk.bin"
    chunk.write_bytes(b"data")
    part = FilePart(payload=chunk, extra=_s3_file_info())

    assert part.get_file() == chunk


class _FailingUploadClient:
    def upload_file(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("upload failed")


def test_basic_ops_upload_file_propagates_client_error(tmp_path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"data")

    with pytest.raises(RuntimeError, match="upload failed"):
        upload_file(
            s3_client=cast(Any, _FailingUploadClient()),
            bucket_name="bucket",
            file_path=src,
            object_name="key",
        )


def test_check_part_size_raises_value_error_for_empty_parts() -> None:
    with pytest.raises(ValueError, match="No parts to upload"):
        _check_part_size([])


def test_check_part_size_raises_value_error_for_undersized_parts() -> None:
    tiny_part = PartInfo.split_parts(SizeSuffix("1MB").as_int(), SizeSuffix("1MB").as_int())

    with pytest.raises(ValueError, match="too small to upload"):
        _check_part_size(tiny_part)


def _stub_info_json(size: int, chunk_size: int, first_part: int, last_part: int) -> InfoJson:
    info = cast(InfoJson, object.__new__(InfoJson))
    info.data = {
        "size": size,
        "chunksize_int": chunk_size,
        "first_part": first_part,
        "last_part": last_part,
    }
    return info


def test_compute_all_parts_slices_by_first_and_last_part_index() -> None:
    info = _stub_info_json(
        size=SizeSuffix("15MB").as_int(),
        chunk_size=SizeSuffix("5MB").as_int(),
        first_part=0,
        last_part=1,
    )

    parts = info.compute_all_parts()

    assert [p.part_number for p in parts] == [1, 2]


def test_fetch_is_done_returns_false_when_computation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = cast(InfoJson, object.__new__(InfoJson))

    def _raise() -> list[int]:
        raise KeyError("first_part")

    monkeypatch.setattr(info, "fetch_remaining_part_numbers", _raise)

    assert info.fetch_is_done() is False


def test_fetch_is_done_returns_true_when_nothing_remains(monkeypatch: pytest.MonkeyPatch) -> None:
    info = cast(InfoJson, object.__new__(InfoJson))
    monkeypatch.setattr(info, "fetch_remaining_part_numbers", list)

    assert info.fetch_is_done() is True
