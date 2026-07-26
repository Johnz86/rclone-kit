"""Unit tests for `rclone_kit.s3.multipart.upload_state.UploadState`.

Regression coverage for a real gap found in review: `add_finished()` used to
call `_save_no_lock()` directly, bypassing both the module-level
`_SAVE_STATE_LOCK` and the fingerprint check that `save()` performs. If two
`UploadState` instances ever pointed at the same persisted path concurrently
(e.g. a stale runner process left running alongside a freshly resumed one),
one instance's `add_finished()` would silently overwrite the other's
on-disk state instead of detecting the divergence.
"""

from pathlib import Path
from typing import Any, cast

import pytest

from rclone_kit.s3.multipart.finished_piece import FinishedPiece
from rclone_kit.s3.multipart.upload_info import UploadInfo
from rclone_kit.s3.multipart.upload_state import UploadState


def _upload_info(*, file_size: int = 100, chunk_size: int = 10) -> UploadInfo:
    return UploadInfo(
        s3_client=cast(Any, object()),
        bucket_name="bucket",
        object_name="object",
        src_file_path=Path("src.bin"),
        upload_id="upload-id",
        retries=3,
        chunk_size=chunk_size,
        file_size=file_size,
    )


def _part(number: int) -> FinishedPiece:
    return FinishedPiece(part_number=number, etag=f"etag-{number}")


def test_add_finished_persists_the_new_part(tmp_path: Path) -> None:
    persistent = tmp_path / "state.json"
    state = UploadState(upload_info=_upload_info(), peristant=persistent, parts=[])

    state.add_finished(_part(1))

    reloaded = UploadState.from_json(cast(Any, object()), persistent)
    assert reloaded.finished() == 1


def test_add_finished_raises_when_another_instance_diverged_the_persisted_fingerprint(
    tmp_path: Path,
) -> None:
    persistent = tmp_path / "state.json"
    state_a = UploadState(upload_info=_upload_info(file_size=100), peristant=persistent, parts=[])
    state_a.save()

    # A second instance for a different upload (different file_size, hence a
    # different fingerprint) writes directly to the same persisted path,
    # simulating a stale runner process/a retried caller pointed at the same
    # file that wrote before this fix existed to catch it.
    state_b = UploadState(upload_info=_upload_info(file_size=200), peristant=persistent, parts=[])
    persistent.write_text(state_b.to_json_str(), encoding="utf-8")

    with pytest.raises(ValueError, match="fingerprint changed"):
        state_a.add_finished(_part(1))


def test_add_finished_with_none_part_is_a_noop(tmp_path: Path) -> None:
    persistent = tmp_path / "state.json"
    state = UploadState(upload_info=_upload_info(), peristant=persistent, parts=[])

    state.add_finished(cast(Any, None))

    assert state.parts == []
    assert not persistent.exists()
