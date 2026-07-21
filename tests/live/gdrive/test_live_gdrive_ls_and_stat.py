"""Live test file.

Read-only listing and metadata probes, plus the write_text()/read_text()
round-trip that confirms Drive auto-creates a missing folder on first
write, unlike the Ceph bucket `tests/live/s3` needed an explicit mkdir for.
"""

import pytest

from rclone_kit import DirListing, Rclone

pytestmark = pytest.mark.live_gdrive


def test_ls_lists_the_remote_root(live_rclone: Rclone, live_remote_name: str) -> None:
    """Only a type/shape smoke check: unlike the S3 suite, this remote's
    root contents are the real user's actual Drive, so its emptiness or
    otherwise can't be asserted on without being flaky."""
    listing = live_rclone.ls(f"{live_remote_name}:", max_depth=0)

    assert isinstance(listing, DirListing)
    assert isinstance(listing.dirs, list)
    assert isinstance(listing.files, list)


def test_write_text_creates_the_folder_and_round_trips(
    live_rclone: Rclone, live_test_prefix: str
) -> None:
    path = f"{live_test_prefix}/probe.txt"

    assert live_rclone.exists(live_test_prefix) is False

    live_rclone.write_text("probe contents", path)

    assert live_rclone.exists(live_test_prefix) is True
    assert live_rclone.read_text(path) == "probe contents"


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
