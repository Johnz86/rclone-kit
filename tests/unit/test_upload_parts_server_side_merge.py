"""Unit tests for `rclone_kit.s3.multipart.upload_parts_server_side_merge`."""

import subprocess
from types import SimpleNamespace
from typing import Any, cast

import pytest

from rclone_kit.completed_process import CompletedProcess
from rclone_kit.exceptions import S3MergeError
from rclone_kit.rclone_impl import RcloneImpl
from rclone_kit.s3.multipart.info_json import InfoJson
from rclone_kit.s3.multipart.merge_state import MergeState, Part
from rclone_kit.s3.multipart.upload_parts_server_side_merge import (
    S3MultiPartMerger,
    WriteMergeStateThread,
    _cleanup_merge,
    _complete_multipart_upload_from_parts,
    _upload_part_copy_task,
)


def _stub_rclone_impl() -> RcloneImpl:
    return object.__new__(RcloneImpl)


def _stub_merge_state() -> MergeState:
    return MergeState(
        rclone_impl=_stub_rclone_impl(),
        merge_path="merge.json",
        upload_id="upload-id",
        bucket="bucket",
        dst_key="dst-key",
        finished=[],
        all_parts=[Part(part_number=1, s3_key="key")],
    )


def _stub_info(
    size: int = 100, dst: str = "remote:bucket/dst", parts_dir: str = "remote:bucket/parts"
) -> InfoJson:
    return cast(InfoJson, SimpleNamespace(size=size, dst=dst, parts_dir=parts_dir))


class _FailingS3Client:
    def upload_part_copy(self, **_kwargs: object) -> object:
        raise RuntimeError("copy failed")

    def complete_multipart_upload(self, **_kwargs: object) -> object:
        raise RuntimeError("complete failed")


def test_upload_part_copy_task_raises_last_error_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "rclone_kit.s3.multipart.upload_parts_server_side_merge.time.sleep", lambda _s: None
    )
    monkeypatch.setattr(
        "rclone_kit.s3.multipart.upload_parts_server_side_merge._DEFAULT_PART_COPY_RETRIES", 0
    )

    with pytest.raises(RuntimeError, match="copy failed"):
        _upload_part_copy_task(
            s3_client=cast(Any, _FailingS3Client()),
            state=_stub_merge_state(),
            source_bucket="bucket",
            source_key="key",
            part_number=1,
        )


def test_complete_multipart_upload_raises_s3_merge_error() -> None:
    with pytest.raises(S3MergeError):
        _complete_multipart_upload_from_parts(
            s3_client=cast(Any, _FailingS3Client()),
            state=_stub_merge_state(),
            finished_parts=[],
        )


def test_cleanup_merge_raises_file_not_found_when_destination_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rclone = _stub_rclone_impl()
    monkeypatch.setattr(rclone, "exists", lambda _src: False)

    with pytest.raises(FileNotFoundError):
        _cleanup_merge(rclone=rclone, info=_stub_info())


def test_cleanup_merge_raises_value_error_on_size_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rclone = _stub_rclone_impl()
    monkeypatch.setattr(rclone, "exists", lambda _src: True)
    monkeypatch.setattr(rclone, "size_file", lambda _src: 1)

    with pytest.raises(ValueError):
        _cleanup_merge(rclone=rclone, info=_stub_info(size=100))


def test_cleanup_merge_raises_s3_merge_error_when_purge_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rclone = _stub_rclone_impl()
    monkeypatch.setattr(rclone, "exists", lambda _src: True)
    monkeypatch.setattr(rclone, "size_file", lambda _src: 100)
    monkeypatch.setattr(
        rclone,
        "purge",
        lambda _src: CompletedProcess.from_subprocess(
            subprocess.CompletedProcess(args=["rclone", "purge"], returncode=1)
        ),
    )

    with pytest.raises(S3MergeError):
        _cleanup_merge(rclone=rclone, info=_stub_info(size=100))


def _stub_merger() -> S3MultiPartMerger:
    merger = cast(S3MultiPartMerger, object.__new__(S3MultiPartMerger))
    merger.rclone_impl = _stub_rclone_impl()
    merger.state = _stub_merge_state()
    merger.write_thread = None
    merger.client = cast(Any, object())
    merger.max_workers = 1
    merger.verbose = False
    merger._closed = False
    return merger


def test_write_merge_state_thread_close_is_idempotent_with_nothing_queued() -> None:
    thread = WriteMergeStateThread(
        rclone_impl=_stub_rclone_impl(), merge_state=_stub_merge_state(), verbose=False
    )

    thread.close()
    thread.close()

    assert not thread.is_alive()


def test_merge_closes_write_thread_when_do_upload_task_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(**_kwargs: object) -> None:
        raise RuntimeError("part copy failed")

    monkeypatch.setattr(
        "rclone_kit.s3.multipart.upload_parts_server_side_merge._do_upload_task", _raise
    )
    merger = _stub_merger()

    with pytest.raises(RuntimeError, match="part copy failed"):
        merger.merge()

    assert merger.write_thread is not None
    assert not merger.write_thread.is_alive()


def test_merge_closes_write_thread_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "rclone_kit.s3.multipart.upload_parts_server_side_merge._do_upload_task",
        lambda **_kwargs: None,
    )
    merger = _stub_merger()

    merger.merge()

    assert merger.write_thread is not None
    assert not merger.write_thread.is_alive()
