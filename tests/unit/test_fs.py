"""
UUnit test file for the DB class.
"""

import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from rclone_kit.dir_listing import DirListing
from rclone_kit.exceptions import FilesystemError
from rclone_kit.file import File
from rclone_kit.fs.filesystem import FSPath, RealFS, RemoteFS
from rclone_kit.http_server import HttpServer
from rclone_kit.operation import OperationResult
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath

HERE = Path(__file__).parent
DB_PATH = HERE / "test.db"

os.environ["DB_PATH"] = str(DB_PATH)

_BACKSLASH_NAME = "weird" + chr(92) + "name.txt"


def _bare_remote_fs() -> RemoteFS:
    fs = object.__new__(RemoteFS)
    fs.shutdown = True
    return fs


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


def test_rmtree_raises_file_not_found_for_missing_path() -> None:
    with TemporaryDirectory() as temp_dir:
        missing = RealFS.from_path(Path(temp_dir) / "does-not-exist")
        with pytest.raises(FileNotFoundError):
            missing.rmtree()


def test_rmtree_with_ignore_errors_is_a_no_op_for_a_missing_path() -> None:
    with TemporaryDirectory() as temp_dir:
        missing = RealFS.from_path(Path(temp_dir) / "does-not-exist")
        missing.rmtree(ignore_errors=True)


def test_remote_fs_path_truediv_preserves_literal_backslash_in_joined_name() -> None:
    """`FSPath` path math must use `PurePosixPath`, not `Path`, for a
    `RemoteFS`-backed path: `remote:bucket/...` is a forward-slash-only
    rclone path, never a local filesystem path, so a literal `\\` (a valid
    character in many remote object keys) must never be treated as a
    directory separator the way `WindowsPath` would on Windows.
    """
    parent = FSPath(_bare_remote_fs(), "remote:bucket/subdir")

    child = parent / _BACKSLASH_NAME

    assert child.path == f"remote:bucket/subdir/{_BACKSLASH_NAME}"


def test_remote_fs_path_relative_to_preserves_literal_backslash() -> None:
    parent = FSPath(_bare_remote_fs(), "remote:bucket/subdir")
    child = FSPath(_bare_remote_fs(), f"remote:bucket/subdir/{_BACKSLASH_NAME}")

    assert child.relative_to(parent).path == _BACKSLASH_NAME


def test_remote_fs_path_name_preserves_literal_backslash() -> None:
    path = FSPath(_bare_remote_fs(), f"remote:bucket/subdir/{_BACKSLASH_NAME}")

    assert path.name == _BACKSLASH_NAME


def test_real_fs_path_truediv_still_uses_native_path_semantics() -> None:
    """A `RealFS`-backed `FSPath` must keep native local-filesystem
    joining (verifying the `RemoteFS` fix above did not change `RealFS`
    behavior).
    """
    with TemporaryDirectory() as temp_dir:
        parent = RealFS.from_path(Path(temp_dir))
        child = parent / "sub" / "file.txt"

        assert child.path == (Path(temp_dir) / "sub" / "file.txt").as_posix()


class FakeRemoteFSAccess:
    """A fake `RemoteFSAccess` (CLI-to-C-ABI migration Wave G design):
    every method the protocol declares, none of which touch a real
    process or network call."""

    def __init__(self) -> None:
        self.serve_http_calls: list[str] = []
        self.exists_result = False
        self.stat_result: File | None = None
        self.ls_result: DirListing | None = None

    def serve_http(self, src: str, addr: str | None = None) -> HttpServer:
        del addr
        self.serve_http_calls.append(src)
        server = object.__new__(HttpServer)
        server.process = None
        return server

    def is_s3(self, dst: str) -> bool:
        del dst
        return False

    def copy_file_s3(self, src: Path, dst: str, verbose: bool | None = None) -> None:
        raise NotImplementedError

    def copy_to(self, src: str, dst: str) -> OperationResult:
        raise NotImplementedError

    def read_bytes(self, src: str) -> bytes:
        raise NotImplementedError

    def write_bytes(self, data: bytes, dst: str) -> None:
        raise NotImplementedError

    def delete_files(self, files: str) -> OperationResult:
        raise NotImplementedError

    def exists(self, src: object) -> bool:
        del src
        return self.exists_result

    def stat(self, src: str) -> File:
        del src
        if self.stat_result is None:
            raise FileNotFoundError("not found")
        return self.stat_result

    def ls(self, src: str, max_depth: int | None = None) -> DirListing:
        del src, max_depth
        assert self.ls_result is not None
        return self.ls_result


def _rpath_item(name: str, *, is_dir: bool) -> RPath:
    access = FakeRemoteFSAccess()
    remote = Remote(name="remote", rclone=access)  # type: ignore[arg-type]
    rpath = RPath(
        remote=remote,
        path=name,
        name=name,
        size=0,
        mime_type="text/plain",
        mod_time="2024-01-01T00:00:00Z",
        is_dir=is_dir,
    )
    rpath.set_rclone(access)  # type: ignore[arg-type]
    return rpath


def _file_item(name: str, *, is_dir: bool) -> File:
    # Matches Rclone.stat()'s own real contract: it always wraps in a
    # `File`, never a `Dir`, even for a directory target - callers check
    # `.path.is_dir`, not the wrapper's own type.
    return File(_rpath_item(name, is_dir=is_dir))


def test_remote_fs_construction_never_calls_serve_http() -> None:
    access = FakeRemoteFSAccess()

    fs = RemoteFS(access, "remote:base")

    assert fs.server is None
    assert access.serve_http_calls == []


def test_remote_fs_serve_starts_the_server_lazily_and_caches_it() -> None:
    access = FakeRemoteFSAccess()
    fs = RemoteFS(access, "remote:base")

    server1 = fs.serve()
    server2 = fs.serve()

    assert access.serve_http_calls == ["remote:base"]
    assert server1 is server2


def test_remote_fs_exists_delegates_to_access_exists() -> None:
    access = FakeRemoteFSAccess()
    access.exists_result = True
    fs = RemoteFS(access, "remote:base")

    assert fs.exists("remote:base/a.txt") is True


def test_remote_fs_is_dir_and_is_file_use_stat() -> None:
    access = FakeRemoteFSAccess()
    access.stat_result = _file_item("sub", is_dir=True)
    fs = RemoteFS(access, "remote:base")

    assert fs.is_dir("remote:base/sub") is True
    assert fs.is_file("remote:base/sub") is False


def test_remote_fs_is_dir_and_is_file_are_false_when_stat_raises() -> None:
    access = FakeRemoteFSAccess()
    fs = RemoteFS(access, "remote:base")

    assert fs.is_dir("remote:base/missing") is False
    assert fs.is_file("remote:base/missing") is False


def test_remote_fs_ls_splits_files_and_dirs_with_trailing_slash_marker() -> None:
    access = FakeRemoteFSAccess()
    access.ls_result = DirListing(
        [_rpath_item("a.txt", is_dir=False), _rpath_item("sub", is_dir=True)]
    )
    fs = RemoteFS(access, "remote:base")

    files, dirs = fs.ls("remote:base")

    assert files == ["a.txt"]
    assert dirs == ["sub/"]


def test_remote_fs_ls_raises_file_not_found_when_access_ls_fails() -> None:
    class _FailingAccess(FakeRemoteFSAccess):
        def ls(self, src: str, max_depth: int | None = None) -> DirListing:
            del src, max_depth
            raise RuntimeError("boom")

    fs = RemoteFS(_FailingAccess(), "remote:base")

    with pytest.raises(FileNotFoundError):
        fs.ls("remote:base")


def test_remote_fs_dispose_is_a_no_op_without_a_started_server() -> None:
    access = FakeRemoteFSAccess()
    fs = RemoteFS(access, "remote:base")

    fs.dispose()
    fs.dispose()

    assert fs.server is None


if __name__ == "__main__":
    unittest.main()
