"""Native-backed proof that `RcloneRcJobClient` correctly parses the real
rclone RC job/status and core/stats wire shapes - not just the fake-based
shapes asserted in `tests/unit/test_rc_jobs.py`.

Skipped automatically when no built native library exists (run
`scripts/native/build.py` first). Does not need the `rclone.exe`
executable: this exercises the RC job boundary only.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from conftest import NATIVE_LIBRARY_AVAILABLE
from rclone_kit.exceptions import JobIdentityError
from rclone_kit.operation import JobState
from rclone_kit.rc.client import RcClient
from rclone_kit.rc.jobs import RcJobNotFoundError, RcJobRef, RcloneRcJobClient

if TYPE_CHECKING:
    from rclone_kit.native.runtime import RcloneRuntime

pytestmark = pytest.mark.skipif(
    not NATIVE_LIBRARY_AVAILABLE,
    reason="No built native library found; run scripts/native/build.py first.",
)


def _job_client(native_runtime: RcloneRuntime) -> RcloneRcJobClient:
    return RcloneRcJobClient(RcClient(native_runtime))


def test_start_status_and_stats_roundtrip_for_a_real_copy(
    tmp_path: Path, native_runtime: RcloneRuntime
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "a.txt").write_bytes(b"x" * 1000)
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_bytes(b"y" * 2000)

    job_client = _job_client(native_runtime)
    group = f"rclone-kit-test/{uuid.uuid4()}"

    ref = job_client.start("sync/copy", {"srcFs": str(src), "dstFs": str(dst)}, group=group)
    assert ref.job_id > 0
    assert ref.execute_id

    status = job_client.status(ref)
    deadline = time.monotonic() + 10
    while not status.state.is_terminal and time.monotonic() < deadline:
        time.sleep(0.05)
        status = job_client.status(ref)

    assert status.state is JobState.SUCCEEDED
    assert status.error is None
    assert status.ended_at is not None

    stats = job_client.stats(group)
    assert stats.total_transfers == 2
    assert stats.bytes == 3000

    job_client.delete_stats(group)
    assert (dst / "a.txt").read_bytes() == b"x" * 1000
    assert (dst / "sub" / "b.txt").read_bytes() == b"y" * 2000


def test_status_for_an_unknown_job_id_raises_rc_job_not_found_error(
    native_runtime: RcloneRuntime,
) -> None:
    job_client = _job_client(native_runtime)
    ref = RcJobRef(job_id=999_999_999, execute_id="does-not-matter", group="g")

    with pytest.raises(RcJobNotFoundError):
        job_client.status(ref)


def test_stop_for_an_unknown_job_id_is_idempotent(native_runtime: RcloneRuntime) -> None:
    job_client = _job_client(native_runtime)
    ref = RcJobRef(job_id=999_999_999, execute_id="does-not-matter", group="g")

    job_client.stop(ref)  # must not raise


def test_execute_id_mismatch_is_detected_against_a_real_job(
    tmp_path: Path, native_runtime: RcloneRuntime
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "a.txt").write_bytes(b"x")

    job_client = _job_client(native_runtime)
    group = f"rclone-kit-test/{uuid.uuid4()}"
    real_ref = job_client.start("sync/copy", {"srcFs": str(src), "dstFs": str(dst)}, group=group)

    deadline = time.monotonic() + 10
    status = job_client.status(real_ref)
    while not status.state.is_terminal and time.monotonic() < deadline:
        time.sleep(0.05)
        status = job_client.status(real_ref)

    forged_ref = RcJobRef(
        job_id=real_ref.job_id, execute_id="not-the-real-execute-id", group=group
    )

    with pytest.raises(JobIdentityError):
        job_client.status(forged_ref)
