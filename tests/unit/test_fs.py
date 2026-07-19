"""
UUnit test file for the DB class.
"""

import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from rclone_kit.exceptions import FilesystemError
from rclone_kit.fs.filesystem import FSPath, RealFS

HERE = Path(__file__).parent
DB_PATH = HERE / "test.db"

os.environ["DB_PATH"] = str(DB_PATH)


class RcloneFSTester(unittest.TestCase):
    """Test DB functionality."""

    def test_os_walk(self) -> None:
        """Walking a real directory tree finds every file and directory.

        Asserts set membership, not order: `RealFS.ls()` lists entries via
        `Path.iterdir()`, whose order is filesystem-dependent (e.g. ext4
        does not return entries in creation or alphabetical order the way
        NTFS commonly does), so no ordering guarantee exists to assert on.
        """
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)

            (path / "sub1").mkdir()
            (path / "sub2").mkdir()

            (path / "file1.txt").touch()
            (path / "file2.txt").touch()

            (path / "sub1" / "subfile1.txt").touch()

            cwd = RealFS.from_path(path)

            all_dirs: list[FSPath] = []
            all_files: list[FSPath] = []

            with cwd.walk_begin() as walker:
                for current_dir, dir_paths, file_paths in walker:
                    for dir_path in dir_paths:
                        full_path = current_dir / dir_path
                        all_dirs.append(full_path)
                    for file_path in file_paths:
                        full_path = current_dir / file_path
                        all_files.append(full_path)

            self.assertCountEqual(
                [fs_path.relative_to(cwd).path for fs_path in all_dirs],
                ["sub1", "sub2"],
            )
            self.assertCountEqual(
                [fs_path.relative_to(cwd).path for fs_path in all_files],
                ["file1.txt", "file2.txt", "sub1/subfile1.txt"],
            )

    def test_with_suffix(self) -> None:
        """Test with_suffix functionality."""
        path: FSPath = RealFS.from_path(HERE / "test.db")
        with_suffix = path.with_suffix(".txt")
        self.assertEqual(with_suffix.path, (HERE / "test.txt").as_posix())

    def test_suffix(self) -> None:
        """Test suffix functionality."""
        path: FSPath = RealFS.from_path(HERE / "test.db")
        suffix = path.suffix
        self.assertEqual(suffix, ".db")

    def test_set_membership(self) -> None:
        path = RealFS.from_path(HERE / "test.db")
        path_set: set[FSPath] = {path}
        self.assertIn(path, path_set)
        self.assertNotIn(RealFS.from_path(HERE / "test.db"), path_set)

    def test_create_and_remove(self) -> None:
        """Test create and remove functionality."""
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "test.txt"
            fspath: FSPath = RealFS.from_path(path)
            self.assertFalse(fspath.exists())
            fspath.write_bytes(b"test")
            self.assertTrue(fspath.exists())
            fspath.remove()
            self.assertFalse(fspath.exists())


def test_unlink_raises_file_not_found_for_missing_file() -> None:
    with TemporaryDirectory() as temp_dir:
        missing = RealFS.from_path(Path(temp_dir) / "does-not-exist.txt")
        with pytest.raises(FileNotFoundError):
            missing.unlink()


def test_remove_raises_file_not_found_for_missing_path() -> None:
    with TemporaryDirectory() as temp_dir:
        missing = RealFS.from_path(Path(temp_dir) / "does-not-exist.txt")
        with pytest.raises(FileNotFoundError):
            missing.remove()


def test_remove_wraps_other_os_errors_in_filesystem_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_permission_error(*_args, **_kwargs):
        raise PermissionError("simulated permission failure")

    with TemporaryDirectory() as temp_dir:
        directory = RealFS.from_path(Path(temp_dir))
        monkeypatch.setattr(shutil, "rmtree", _raise_permission_error)
        try:
            with pytest.raises(FilesystemError) as exc_info:
                directory.remove()
            assert isinstance(exc_info.value.cause, PermissionError)
        finally:
            monkeypatch.undo()


if __name__ == "__main__":
    unittest.main()
