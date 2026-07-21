"""Live test file.

Exercises directory-by-directory traversal (`walk`) and the streaming
recursive listing (`ls_stream`) over a populated test prefix.
"""

from pathlib import Path

import pytest

from rclone_kit import DirListing, FileItem, Rclone

pytestmark = pytest.mark.live


def test_walk_visits_every_directory_in_the_tree(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    live_rclone.copy(str(local_source_tree), live_test_prefix, check=True)

    directories: list[DirListing] = list(live_rclone.walk(live_test_prefix, max_depth=-1))

    all_files = {file.name for directory in directories for file in directory.files}
    assert all_files == {"a.txt", "b.txt", "c.txt"}


def test_ls_stream_pages_every_file_recursively(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    live_rclone.copy(str(local_source_tree), live_test_prefix, check=True)

    with live_rclone.ls_stream(live_test_prefix, max_depth=-1) as stream:
        pages: list[list[FileItem]] = list(stream.files_paged(page_size=2))

    all_files = {file.name for page in pages for file in page}
    assert all_files == {"a.txt", "b.txt", "c.txt"}
