"""Native-backed coverage for the embedded async copy engine
(`start_copy()`, `copy()`, `copy_dir()`, `copy_remote()`) against the real
built native library, including the fork-owned `rclonekit/copy` Go
endpoint.

Skipped automatically when no built native library exists (run
`scripts/native/build.py` first).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from conftest import NATIVE_LIBRARY_AVAILABLE
from rclone_kit.client import Rclone
from rclone_kit.exceptions import (
    OperationFailedError,
    OperationTimeoutError,
)
from rclone_kit.operation import JobState
from rclone_kit.rc.client import RcClient
from rclone_kit.rc.jobs import RcloneRcJobClient
from rclone_kit.remote import Remote

pytestmark = pytest.mark.skipif(
    not NATIVE_LIBRARY_AVAILABLE,
    reason="No built native library found; run scripts/native/build.py first.",
)


def _make_nested_tree(root: Path) -> None:
    root.mkdir()
    (root / "a.txt").write_bytes(b"a" * 100)
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_bytes(b"b" * 200)
    (root / "sub" / "nested").mkdir()
    (root / "sub" / "nested" / "c.txt").write_bytes(b"c" * 300)


def _tree_contents(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_start_copy_for_a_nested_tree(tmp_path: Path, embedded: Rclone) -> None:
    embedded_src = tmp_path / "embedded_src"
    embedded_dst = tmp_path / "embedded_dst"
    _make_nested_tree(embedded_src)

    handle = embedded.start_copy(str(embedded_src), str(embedded_dst))
    result = handle.wait(timeout=15.0)

    assert result.ok is True
    assert result.stats is not None
    assert result.stats.total_transfers == 3
    assert _tree_contents(embedded_dst) == _tree_contents(embedded_src)


def test_start_copy_handles_an_empty_directory(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "empty_src"
    dst = tmp_path / "empty_dst"
    src.mkdir()

    result = embedded.start_copy(str(src), str(dst)).wait(timeout=10.0)

    assert result.ok is True
    assert result.stats is not None
    assert result.stats.total_transfers == 0


def test_start_copy_skips_already_synced_files_on_a_second_pass(
    tmp_path: Path, embedded: Rclone
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_nested_tree(src)

    first = embedded.start_copy(str(src), str(dst)).wait(timeout=10.0)
    second = embedded.start_copy(str(src), str(dst)).wait(timeout=10.0)

    assert first.ok is True
    assert second.ok is True
    assert second.stats is not None
    assert second.stats.transfers == 0


def test_start_copy_fails_for_a_missing_source(tmp_path: Path, embedded: Rclone) -> None:
    missing_src = tmp_path / "does-not-exist"
    dst = tmp_path / "dst"

    result = embedded.start_copy(str(missing_src), str(dst), check=False).wait(timeout=10.0)

    assert result.ok is False
    assert result.error is not None


def test_start_copy_check_true_raises_operation_failed_error(
    tmp_path: Path, embedded: Rclone
) -> None:
    missing_src = tmp_path / "does-not-exist"
    dst = tmp_path / "dst"

    with pytest.raises(OperationFailedError):
        embedded.start_copy(str(missing_src), str(dst), check=True).wait(timeout=10.0)


def test_start_copy_wait_timeout_raises_before_the_first_poll(
    tmp_path: Path, embedded: Rclone
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_nested_tree(src)

    handle = embedded.start_copy(str(src), str(dst))

    # The client's job monitor's default poll interval is well above this
    # timeout, so this deterministically races the first background poll
    # rather than depending on the underlying (near-instant local) copy's
    # own duration.
    with pytest.raises(OperationTimeoutError):
        handle.wait(timeout=0.01)


def test_start_copy_cancel_does_not_crash_and_settles(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "a.bin").write_bytes(b"z" * 2_000_000)

    handle = embedded.start_copy(str(src), str(dst), check=False)
    accepted = handle.cancel()
    result = handle.wait(timeout=15.0)

    assert accepted is True
    # A fast local copy commonly finishes before cancellation lands, so this
    # only asserts the handle settled cleanly either way.
    assert result.ok or result.cancelled or result.error is not None
    assert handle.done


def test_copy_reports_success_and_reproduces_the_tree(tmp_path: Path, embedded: Rclone) -> None:
    embedded_src = tmp_path / "embedded_src"
    embedded_dst = tmp_path / "embedded_dst"
    _make_nested_tree(embedded_src)

    embedded_result = embedded.copy(str(embedded_src), str(embedded_dst))

    assert embedded_result.ok is True
    assert _tree_contents(embedded_dst) == _tree_contents(embedded_src)


def test_copy_dir_never_raises_on_failure(tmp_path: Path, embedded: Rclone) -> None:
    missing_src = tmp_path / "does-not-exist"
    dst = tmp_path / "dst"

    result = embedded.copy_dir(str(missing_src), str(dst))

    assert result.ok is False


def test_copy_remote_never_raises_for_an_unconfigured_remote(embedded: Rclone) -> None:
    # Remote is a bare-root reference ("name:", no path suffix - its
    # constructor rejects a colon in the name), so it cannot address an
    # arbitrary local directory the way a str/Dir target can; copy_dir()'s
    # tests above already prove the shared start_copy() engine works for
    # real local paths. This proves copy_remote()'s own non-raising-on-
    # failure contract via a real (failing) RC round trip.
    src_remote = Remote("does-not-exist-src", embedded)
    dst_remote = Remote("does-not-exist-dst", embedded)

    result = embedded.copy_remote(src_remote, dst_remote)

    assert result.ok is False


def test_job_stats_are_isolated_by_group(tmp_path: Path, embedded: Rclone) -> None:
    src_a = tmp_path / "src_a"
    dst_a = tmp_path / "dst_a"
    src_b = tmp_path / "src_b"
    dst_b = tmp_path / "dst_b"
    src_a.mkdir()
    src_b.mkdir()
    (src_a / "a.txt").write_bytes(b"x" * 1000)
    (src_b / "b1.txt").write_bytes(b"y" * 1000)
    (src_b / "b2.txt").write_bytes(b"y" * 1000)

    handle_a = embedded.start_copy(str(src_a), str(dst_a))
    handle_b = embedded.start_copy(str(src_b), str(dst_b))
    result_a = handle_a.wait(timeout=10.0)
    result_b = handle_b.wait(timeout=10.0)

    assert result_a.stats is not None
    assert result_b.stats is not None
    assert result_a.stats.total_transfers == 1
    assert result_b.stats.total_transfers == 2


def test_status_reaches_succeeded_state(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_nested_tree(src)

    handle = embedded.start_copy(str(src), str(dst))
    handle.wait(timeout=10.0)

    assert handle.status().state is JobState.SUCCEEDED


def test_rc_job_client_used_directly_still_works_end_to_end(
    tmp_path: Path, embedded: Rclone
) -> None:
    # Sanity check that the public facade and the lower-level RcloneRcJobClient
    # agree - both are exercised by the same underlying RcClient.
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_nested_tree(src)

    assert embedded._embedded_runtime is not None
    job_client = RcloneRcJobClient(RcClient(embedded._embedded_runtime))
    ref = job_client.start(
        "rclonekit/copy", {"srcFs": str(src), "dstFs": str(dst)}, group="direct-probe"
    )
    status = job_client.status(ref)
    deadline = time.monotonic() + 10.0
    while not status.state.is_terminal and time.monotonic() < deadline:
        time.sleep(0.05)
        status = job_client.status(ref)

    assert status.state is JobState.SUCCEEDED
