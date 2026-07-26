"""Native-backed test for `upload_parts_resumable()`
(`copy_file_s3_resumable`) under embedded execution.

`upload_parts_resumable()` calls `self.size_file(src)` and
`self.serve_http(src_dir)` directly on its `access: MultipartAccess`
parameter - both embedded-capable - then downloads byte ranges from that
server and re-uploads each chunk via `access.copy_to(chunk, dst_part)`.
None of that machinery is S3-specific: using a plain local directory as
`dst_dir` here exercises the exact same `size_file`/`serve_http`/
range-download/`copy_to` chain the real S3 flow uses, without needing a
real bucket.

What this test deliberately does NOT cover: `upload_parts_server_side_merge
.py`'s merge step, which calls a real `boto3` client's `upload_part_copy`/
`complete_multipart_upload` against an actual S3-compatible bucket - that
step has no local equivalent and (per this repo's own existing
`tests/cloud/test_copy_file_resumable_s3.py::test_copy_parts`) is an
unconditionally-skipped manual test even when live credentials are
available.

Skipped automatically when no built native library exists (run
`scripts/native/build.py` first).
"""

from pathlib import Path

import pytest
from conftest import NATIVE_LIBRARY_AVAILABLE

from rclone_kit.client import Rclone
from rclone_kit.operations.copy_file_parts_resumable import copy_file_parts_resumable
from rclone_kit.s3.multipart.upload_parts_resumable import upload_parts_resumable
from rclone_kit.types import PartInfo, SizeSuffix

pytestmark = pytest.mark.skipif(
    not NATIVE_LIBRARY_AVAILABLE,
    reason="No built native library found; run scripts/native/build.py first.",
)

_PART_SIZE = SizeSuffix("5M")


def test_upload_parts_resumable_reassembles_correctly_under_embedded_execution(
    tmp_path: Path, embedded: Rclone
) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src_path = src_dir / "source.bin"
    payload = bytes((i % 251) for i in range(12 * 1024 * 1024))  # 12MiB, not part-size-aligned
    src_path.write_bytes(payload)

    dst_dir = tmp_path / "dst-parts"
    dst_dir.mkdir()
    part_infos = PartInfo.split_parts(len(payload), _PART_SIZE)
    assert len(part_infos) == 3  # 5M + 5M + ~2M, exercising a partial final part

    upload_parts_resumable(
        embedded,
        src=str(src_path),
        dst_dir=str(dst_dir),
        part_infos=part_infos,
        threads=2,
    )

    uploaded_parts = sorted(p for p in dst_dir.iterdir() if p.name != "info.json")
    assert len(uploaded_parts) == 3

    reassembled = b"".join(part.read_bytes() for part in uploaded_parts)
    assert reassembled == payload


def test_upload_parts_resumable_skips_parts_already_recorded_as_done(
    tmp_path: Path, embedded: Rclone
) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src_path = src_dir / "source.bin"
    payload = bytes((i % 251) for i in range(12 * 1024 * 1024))
    src_path.write_bytes(payload)

    dst_dir = tmp_path / "dst-parts"
    dst_dir.mkdir()
    part_infos = PartInfo.split_parts(len(payload), _PART_SIZE)

    upload_parts_resumable(
        embedded, src=str(src_path), dst_dir=str(dst_dir), part_infos=part_infos, threads=2
    )
    first_pass_parts = {p.name for p in dst_dir.iterdir() if p.name != "info.json"}

    # A second call with the same info.json in place should see every part
    # already finished and re-upload nothing.
    for part in dst_dir.iterdir():
        if part.name != "info.json":
            part.write_bytes(b"")

    upload_parts_resumable(
        embedded, src=str(src_path), dst_dir=str(dst_dir), part_infos=part_infos, threads=2
    )
    second_pass_parts = {p.name for p in dst_dir.iterdir() if p.name != "info.json"}

    assert second_pass_parts == first_pass_parts
    assert all((dst_dir / name).read_bytes() == b"" for name in second_pass_parts)


def test_copy_file_parts_resumable_uploads_for_real_under_embedded_execution(
    tmp_path: Path, embedded: Rclone, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`copy_file_s3_resumable()`'s full orchestration (`copy_file_parts_
    resumable`), with only the S3-only merge step faked out - the upload
    half runs for real against the embedded client."""
    pytest.importorskip("boto3")
    merge_calls: list[str] = []
    monkeypatch.setattr(
        "rclone_kit.s3.multipart.upload_parts_server_side_merge.s3_server_side_multi_part_merge",
        lambda **kwargs: merge_calls.append(str(kwargs["info_path"])),
    )

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src_path = src_dir / "source.bin"
    payload = bytes((i % 251) for i in range(12 * 1024 * 1024))
    src_path.write_bytes(payload)

    dst_dir = tmp_path / "dst-parts"
    dst_dir.mkdir()

    copy_file_parts_resumable(
        access=embedded,
        src=str(src_path),
        dst_dir=str(dst_dir),
        part_infos=PartInfo.split_parts(len(payload), _PART_SIZE),
        upload_threads=2,
        merge_threads=1,
    )

    assert merge_calls == [f"{dst_dir}/info.json"]
    uploaded_parts = sorted(p for p in dst_dir.iterdir() if p.name != "info.json")
    reassembled = b"".join(part.read_bytes() for part in uploaded_parts)
    assert reassembled == payload
