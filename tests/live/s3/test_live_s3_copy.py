"""Live test file.

Exercises the transfer surface (`copy_to`, `copy`, `copy_files`,
`is_synced`, `size_files`) against the scoped test prefix.
"""

import time
from itertools import pairwise
from pathlib import Path

import pytest

from rclone_kit import Rclone

pytestmark = pytest.mark.live_s3


def test_copy_to_transfers_a_single_file(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    dst = f"{live_test_prefix}/a.txt"

    result = live_rclone.copy_to(str(local_source_tree / "a.txt"), dst, check=True)

    assert result.ok
    assert live_rclone.read_text(dst) == "alpha"


def test_copy_transfers_a_directory_tree(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    result = live_rclone.copy(str(local_source_tree), live_test_prefix, check=True)

    assert result.ok
    listing = live_rclone.ls(live_test_prefix, max_depth=-1)
    names = {file.name for file in listing.files}
    assert names == {"a.txt", "b.txt", "c.txt"}


def test_copy_files_transfers_a_selected_subset(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    result = live_rclone.copy_files(
        src=str(local_source_tree),
        dst=live_test_prefix,
        files=["a.txt", "nested/c.txt"],
        check=True,
    )

    assert result.ok
    assert live_rclone.exists(f"{live_test_prefix}/a.txt")
    assert live_rclone.exists(f"{live_test_prefix}/nested/c.txt")
    assert not live_rclone.exists(f"{live_test_prefix}/b.txt")


def test_start_copy_watch_reports_consistent_progress_and_settles(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    """`watch()` is the recommended progress idiom over `start_copy()`.

    `local_source_tree` is tiny, so a fast connection may legitimately
    transfer it between two `watch()` samples - only internal consistency
    (bytes never decreasing across samples) is asserted on whatever
    intermediate snapshots were actually observed; the final settled state
    is asserted hard regardless.
    """
    handle = live_rclone.start_copy(str(local_source_tree), live_test_prefix)

    snapshots = list(handle.watch(interval=0.2))

    assert snapshots  # the final snapshot is always yielded, even with zero intermediate ones
    for previous, current in pairwise(snapshots):
        assert current.bytes >= previous.bytes

    result = handle.wait()
    assert result.ok
    listing = live_rclone.ls(live_test_prefix, max_depth=-1)
    names = {file.name for file in listing.files}
    assert names == {"a.txt", "b.txt", "c.txt"}


def test_start_copy_manual_polling_loop_still_works(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    """Regression coverage for the still-supported low-level primitive
    (`docs/production_usage.md`'s documented `while not handle.done` loop) -
    `watch()` is additive, not a replacement."""
    handle = live_rclone.start_copy(str(local_source_tree), live_test_prefix)

    while not handle.done:
        handle.stats()
        time.sleep(0.1)

    result = handle.wait()
    assert result.ok


def test_is_synced_is_true_after_a_full_copy_and_false_after_a_local_change(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    live_rclone.copy(str(local_source_tree), live_test_prefix, check=True)
    assert live_rclone.is_synced(str(local_source_tree), live_test_prefix)

    (local_source_tree / "a.txt").write_text("alpha-changed")
    assert not live_rclone.is_synced(str(local_source_tree), live_test_prefix)


def test_size_files_reports_total_and_individual_sizes(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    live_rclone.copy(str(local_source_tree), live_test_prefix, check=True)

    result = live_rclone.size_files(
        src=live_test_prefix,
        files=["a.txt", "b.txt"],
        check=True,
    )

    assert result.total_size == len(b"alpha") + len(b"bravo")
    assert result.file_sizes["a.txt"] == len(b"alpha")
    assert result.file_sizes["b.txt"] == len(b"bravo")
