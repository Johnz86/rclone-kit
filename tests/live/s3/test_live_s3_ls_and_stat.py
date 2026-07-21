"""Live test file.

Read-only listing and metadata probes against the real remote. These never
write or delete anything, so they run against `LIVE_REMOTE` directly rather
than a disposable test prefix.
"""

import pytest

from rclone_kit import Rclone

pytestmark = pytest.mark.live_s3


def test_ls_lists_the_remote_root(live_rclone: Rclone, live_remote_name: str) -> None:
    listing = live_rclone.ls(f"{live_remote_name}:", max_depth=0)

    assert len(listing.dirs) > 0


def test_stat_and_size_file_agree_on_a_written_object(
    live_rclone: Rclone, live_test_prefix: str
) -> None:
    path = f"{live_test_prefix}/probe.txt"
    live_rclone.write_text("probe contents", path)

    file = live_rclone.stat(path)
    size = live_rclone.size_file(path)

    assert file.name == "probe.txt"
    assert size.as_int() == len(b"probe contents")


def test_exists_is_false_for_a_path_that_was_never_written(
    live_rclone: Rclone, live_test_prefix: str
) -> None:
    assert live_rclone.exists(f"{live_test_prefix}/never-written.txt") is False


def test_stat_raises_file_not_found_for_a_missing_object(
    live_rclone: Rclone, live_test_prefix: str
) -> None:
    with pytest.raises(FileNotFoundError):
        live_rclone.stat(f"{live_test_prefix}/missing.txt")


def test_size_file_raises_file_not_found_for_a_missing_object(
    live_rclone: Rclone, live_test_prefix: str
) -> None:
    with pytest.raises(FileNotFoundError):
        live_rclone.size_file(f"{live_test_prefix}/missing.txt")
