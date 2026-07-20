"""Unit tests for `rclone_kit.s3.multipart.upload_parts_server_side_merge`."""

import json
import subprocess
from types import SimpleNamespace
from typing import Any, cast

import pytest

from rclone_kit.client import Rclone
from rclone_kit.completed_process import CompletedProcess
from rclone_kit.exceptions import S3MergeError
from rclone_kit.s3.multipart.info_json import InfoJson
from rclone_kit.s3.multipart.merge_state import MergeState, Part
from rclone_kit.s3.multipart.upload_parts_server_side_merge import (
    S3MultiPartMerger,
    WriteMergeStateThread,
    _begin_or_resume_merge,
    _cleanup_merge,
    _complete_multipart_upload_from_parts,
    _upload_part_copy_task,
)
from rclone_kit.s3.types import S3Credentials, S3Provider


def _stub_rclone() -> Rclone:
    return object.__new__(Rclone)


def _stub_merge_state() -> MergeState:
    return MergeState(
        rclone=_stub_rclone(),
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
    rclone = _stub_rclone()
    monkeypatch.setattr(rclone, "exists", lambda _src: False)

    with pytest.raises(FileNotFoundError):
        _cleanup_merge(rclone=rclone, info=_stub_info())


def test_cleanup_merge_raises_value_error_on_size_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rclone = _stub_rclone()
    monkeypatch.setattr(rclone, "exists", lambda _src: True)
    monkeypatch.setattr(rclone, "size_file", lambda _src: 1)

    with pytest.raises(ValueError):
        _cleanup_merge(rclone=rclone, info=_stub_info(size=100))


def test_cleanup_merge_raises_s3_merge_error_when_purge_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rclone = _stub_rclone()
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
    merger.rclone = _stub_rclone()
    merger.state = _stub_merge_state()
    merger.write_thread = None
    merger.client = cast(Any, object())
    merger.max_workers = 1
    merger.verbose = False
    merger._closed = False
    return merger


def test_write_merge_state_thread_close_is_idempotent_with_nothing_queued() -> None:
    thread = WriteMergeStateThread(
        rclone=_stub_rclone(),
        merge_state=_stub_merge_state(),
        verbose=False,
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


def test_merge_closes_write_thread_after_retry_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises the exact scenario the write-thread ownership fix targets:
    a part copy exhausts its retries, `_do_upload_task` cancels the
    not-yet-started EOS-sending future via `executor.shutdown(...,
    cancel_futures=True)`, and `merge()` must still close the write thread
    itself rather than leaving it blocked on `queue.get()` forever.
    """
    monkeypatch.setattr(
        "rclone_kit.s3.multipart.upload_parts_server_side_merge.time.sleep", lambda _s: None
    )
    monkeypatch.setattr(
        "rclone_kit.s3.multipart.upload_parts_server_side_merge._DEFAULT_PART_COPY_RETRIES", 0
    )
    merger = _stub_merger()
    merger.client = cast(Any, _FailingS3Client())

    with pytest.raises(RuntimeError, match="copy failed"):
        merger.merge()

    assert merger.write_thread is not None
    assert not merger.write_thread.is_alive()


class _CompletionFailingS3Client:
    def upload_part_copy(self, **_kwargs: object) -> dict:
        return {"CopyPartResult": {"ETag": "etag-1"}}

    def complete_multipart_upload(self, **_kwargs: object) -> None:
        raise RuntimeError("complete failed")


def test_merge_closes_write_thread_after_completion_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every part copy can succeed and still leave the write thread stranded
    if completing the multipart upload itself fails - a separate failure
    point from part-copy retry exhaustion, so merge() must close the write
    thread on this path too.
    """
    rclone = _stub_rclone()
    monkeypatch.setattr(rclone, "write_text", lambda *_args, **_kwargs: None)
    merger = _stub_merger()
    merger.rclone = rclone
    merger.client = cast(Any, _CompletionFailingS3Client())

    with pytest.raises(S3MergeError, match="Failed to complete multipart upload"):
        merger.merge()

    assert merger.write_thread is not None
    assert not merger.write_thread.is_alive()


def _fake_s3_credentials() -> S3Credentials:
    return S3Credentials(
        bucket_name="bucket",
        provider=S3Provider.S3,
        access_key_id="fake-access-key-id",
        secret_access_key="fake-secret-access-key",  # noqa: S106
    )


class _FakeS3ClientForNewUpload:
    def create_multipart_upload(self, **_kwargs: object) -> dict:
        return {"UploadId": "new-upload-id"}


def _stub_info_for_begin_or_resume() -> InfoJson:
    return cast(
        InfoJson,
        SimpleNamespace(
            dst="remote:bucket/dst",
            src_info="remote:bucket/dst-parts/info.json",
            parts_dir="remote:bucket/dst-parts",
            fetch_is_done=lambda: True,
            fetch_all_finished=lambda: ["part.00001_0-100"],
            first_part=1,
            last_part=1,
            dst_name="dst",
        ),
    )


def test_begin_or_resume_merge_falls_back_to_fresh_merge_on_corrupt_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed prior merge.json must not abort the merge: `MergeState.from_json`
    raises `KeyError`/`MergeStateError`, which `_begin_or_resume_merge` treats as "no
    usable prior state" and falls back to starting a fresh merge instead.
    """
    rclone = _stub_rclone()
    monkeypatch.setattr(rclone, "get_s3_credentials", lambda **_kwargs: _fake_s3_credentials())
    monkeypatch.setattr(rclone, "read_text", lambda _path: "{}")
    monkeypatch.setattr(
        "rclone_kit.s3.multipart.upload_parts_server_side_merge.create_s3_client",
        lambda **_kwargs: _FakeS3ClientForNewUpload(),
    )

    with pytest.warns(UserWarning, match="Failed to resume merge"):
        merger = _begin_or_resume_merge(rclone=rclone, info=_stub_info_for_begin_or_resume())

    assert merger.state is not None
    assert merger.state.upload_id == "new-upload-id"
    assert merger.state.finished == []


class _CreateMultipartUploadForbiddenS3Client:
    def create_multipart_upload(self, **_kwargs: object) -> dict:
        raise AssertionError("resume path must not start a new multipart upload")


def _valid_merge_state_json() -> str:
    return json.dumps(
        {
            "merge_path": "remote:bucket/dst-parts/merge.json",
            "bucket": "bucket",
            "dst_key": "dst-dir/dst",
            "upload_id": "resumed-upload-id",
            "finished": [{"etag": "etag-1", "part_number": 1}],
            "all": [
                {"part_number": 1, "s3_key": "dst-parts/part.00001"},
                {"part_number": 2, "s3_key": "dst-parts/part.00002"},
            ],
        }
    )


def test_begin_or_resume_merge_resumes_from_valid_prior_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid prior merge.json must resume rather than restart: the loaded
    state's already-finished parts are preserved, only the remaining part
    is left to copy, and no new multipart upload is created.
    """
    rclone = _stub_rclone()
    monkeypatch.setattr(rclone, "get_s3_credentials", lambda **_kwargs: _fake_s3_credentials())
    monkeypatch.setattr(rclone, "read_text", lambda _path: _valid_merge_state_json())
    monkeypatch.setattr(
        "rclone_kit.s3.multipart.upload_parts_server_side_merge.create_s3_client",
        lambda **_kwargs: _CreateMultipartUploadForbiddenS3Client(),
    )

    merger = _begin_or_resume_merge(rclone=rclone, info=_stub_info_for_begin_or_resume())

    assert merger.state is not None
    assert merger.state.upload_id == "resumed-upload-id"
    assert [p.part_number for p in merger.state.finished] == [1]
    assert merger.state.remaining_parts() == [Part(part_number=2, s3_key="dst-parts/part.00002")]
