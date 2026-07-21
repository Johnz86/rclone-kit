"""Live test file.

Exercises `delete_files` and `purge` directly. Every object touched here is
created by the test itself first, under the disposable `live_test_prefix`.
"""

from pathlib import Path

import pytest

from rclone_kit import Rclone

pytestmark = pytest.mark.live


def test_delete_files_removes_a_single_path(live_rclone: Rclone, live_test_prefix: str) -> None:
    path = f"{live_test_prefix}/solo.txt"
    live_rclone.write_text("solo", path)
    assert live_rclone.exists(path)

    result = live_rclone.delete_files(path, check=True)

    assert result.ok
    assert not live_rclone.exists(path)


def test_delete_files_removes_a_list_of_paths(live_rclone: Rclone, live_test_prefix: str) -> None:
    first = f"{live_test_prefix}/first.txt"
    second = f"{live_test_prefix}/second.txt"
    live_rclone.write_text("first", first)
    live_rclone.write_text("second", second)

    result = live_rclone.delete_files([first, second], check=True)

    assert result.ok
    assert not live_rclone.exists(first)
    assert not live_rclone.exists(second)


def test_purge_removes_a_directory_and_its_contents(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    live_rclone.copy(str(local_source_tree), live_test_prefix, check=True)
    assert live_rclone.exists(f"{live_test_prefix}/a.txt")

    live_rclone.purge(live_test_prefix)

    assert not live_rclone.exists(f"{live_test_prefix}/a.txt")
    assert not live_rclone.exists(f"{live_test_prefix}/nested/c.txt")
