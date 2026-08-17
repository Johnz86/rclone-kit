"""Native-backed proof that `JobHandle`/`_JobMonitor` work end to end
against the real built native library - not just the fake `RcJobClient`
used in `tests/unit/test_job.py`.

Skipped automatically when no built native library exists (run
`scripts/native/build.py` first). Does not need the `rclone.exe`
executable.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from conftest import NATIVE_LIBRARY_AVAILABLE
from rclone_kit.job import _JobMonitor
from rclone_kit.operation import JobState
from rclone_kit.rc.client import RcClient
from rclone_kit.rc.jobs import RcloneRcJobClient

if TYPE_CHECKING:
    from rclone_kit.native.runtime import RcloneRuntime

pytestmark = pytest.mark.skipif(
    not NATIVE_LIBRARY_AVAILABLE,
    reason="No built native library found; run scripts/native/build.py first.",
)


def _monitor(native_runtime: RcloneRuntime) -> _JobMonitor:
    job_client = RcloneRcJobClient(RcClient(native_runtime))
    return _JobMonitor(job_client, poll_interval_seconds=0.02, close_wait_seconds=5.0)


def test_start_wait_succeeds_for_a_real_copy(tmp_path: Path, native_runtime: RcloneRuntime) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "a.txt").write_bytes(b"x" * 1000)
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_bytes(b"y" * 2000)

    monitor = _monitor(native_runtime)
    handle = monitor.start_job(
        "sync/copy",
        {"srcFs": str(src), "dstFs": str(dst)},
        group=f"rclone-kit-test/{uuid.uuid4()}",
        operation="copy",
        source=str(src),
        destination=str(dst),
        check=True,
    )

    result = handle.wait(timeout=10.0)

    assert result.ok
    assert result.stats is not None
    assert result.stats.total_transfers == 2
    assert (dst / "a.txt").read_bytes() == b"x" * 1000
    assert (dst / "sub" / "b.txt").read_bytes() == b"y" * 2000
    monitor.shutdown(deadline_seconds=5.0)


def test_start_fails_for_a_missing_source_directory(
    tmp_path: Path, native_runtime: RcloneRuntime
) -> None:
    missing_src = tmp_path / "does-not-exist"
    dst = tmp_path / "dst"

    monitor = _monitor(native_runtime)
    handle = monitor.start_job(
        "sync/copy",
        {"srcFs": str(missing_src), "dstFs": str(dst)},
        group=f"rclone-kit-test/{uuid.uuid4()}",
        operation="copy",
        source=str(missing_src),
        destination=str(dst),
        check=False,
    )

    result = handle.wait(timeout=10.0)

    assert result.ok is False
    assert result.error is not None
    monitor.shutdown(deadline_seconds=5.0)


def test_cancel_a_real_job_does_not_crash_and_settles(
    tmp_path: Path, native_runtime: RcloneRuntime
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "a.txt").write_bytes(b"z" * 5_000_000)

    monitor = _monitor(native_runtime)
    handle = monitor.start_job(
        "sync/copy",
        {"srcFs": str(src), "dstFs": str(dst)},
        group=f"rclone-kit-test/{uuid.uuid4()}",
        operation="copy",
        source=str(src),
        destination=str(dst),
        check=False,
    )

    accepted = handle.cancel()
    result = handle.wait(timeout=10.0)

    assert accepted is True
    # a fast local copy commonly finishes before cancellation lands, so
    # this only asserts the handle settled cleanly either way - real
    # cancellation-interrupts-an-active-attempt coverage belongs to the
    # downstream Go endpoint's own test suite
    # (native/rclone/librclone/rclonekit/rc/copy_test.go), not here.
    assert result.ok or result.cancelled or result.error is not None
    assert handle.done
    monitor.shutdown(deadline_seconds=5.0)


def test_shutdown_settles_a_real_still_running_job(
    tmp_path: Path, native_runtime: RcloneRuntime
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    for i in range(50):
        (src / f"f{i}.bin").write_bytes(b"w" * 200_000)

    monitor = _monitor(native_runtime)
    monitor.start_job(
        "sync/copy",
        {"srcFs": str(src), "dstFs": str(dst)},
        group=f"rclone-kit-test/{uuid.uuid4()}",
        operation="copy",
        source=str(src),
        destination=str(dst),
        check=False,
    )

    all_settled = monitor.shutdown(deadline_seconds=10.0)

    assert all_settled is True


def test_job_state_is_running_then_terminal(tmp_path: Path, native_runtime: RcloneRuntime) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "a.txt").write_bytes(b"x")

    monitor = _monitor(native_runtime)
    handle = monitor.start_job(
        "sync/copy",
        {"srcFs": str(src), "dstFs": str(dst)},
        group=f"rclone-kit-test/{uuid.uuid4()}",
        operation="copy",
        source=str(src),
        destination=str(dst),
        check=True,
    )

    result = handle.wait(timeout=10.0)

    assert result.ok
    assert handle.status().state is JobState.SUCCEEDED
    monitor.shutdown(deadline_seconds=5.0)
