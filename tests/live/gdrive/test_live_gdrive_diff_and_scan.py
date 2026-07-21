"""Live test file.

Exercises streaming comparison (`diff`) and missing-folder discovery
(`scan_missing_folders`) against a real, structurally different second
backend. This deliberately re-covers the `stream_diff()` `--log-file` fix
(see `src/rclone_kit/operations/listing_ops.py`): the bug it fixed was
masked for years by tests/cloud always naming its missing directory
literally "does-not-exist", so a second real backend hedges against
another backend-specific parsing surprise.
"""

from pathlib import Path

import pytest

from rclone_kit import DiffItem, DiffOption, DiffType, Dir, Rclone

pytestmark = pytest.mark.live_gdrive


def test_diff_reports_equal_after_a_full_copy(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    live_rclone.copy(str(local_source_tree), live_test_prefix, check=True)

    items: list[DiffItem] = list(
        live_rclone.diff(str(local_source_tree), live_test_prefix, fast_list=False)
    )

    assert len(items) == 3
    assert all(item.type is DiffType.EQUAL for item in items)


def test_diff_missing_on_dst_reports_every_local_file_before_any_copy(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    items: list[DiffItem] = list(
        live_rclone.diff(
            str(local_source_tree),
            live_test_prefix,
            diff_option=DiffOption.MISSING_ON_DST,
            fast_list=False,
        )
    )

    assert len(items) == 3
    assert all(item.type is DiffType.MISSING_ON_DST for item in items)


def test_scan_missing_folders_finds_the_nested_directory_before_any_copy(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    """Writes a sibling file first so `live_test_prefix` itself exists:
    unlike an S3 prefix, a Drive folder is a real object, and listing a
    dst whose root doesn't exist at all hits the same generic-path gap
    `test_stat_raises_file_not_found_for_a_missing_object` documents -
    `scan_missing_folders`'s background thread silently swallows the
    resulting `CalledProcessError` and yields nothing instead of raising
    or treating it as "everything is missing". Only the intended
    "existing root, missing nested subdirectory" case is exercised here."""
    live_rclone.write_text("sibling", f"{live_test_prefix}/sibling.txt")

    missing: list[Dir] = list(
        live_rclone.scan_missing_folders(src=str(local_source_tree), dst=live_test_prefix)
    )

    assert any(directory.name == "nested" for directory in missing)
