"""Unit tests for `rclone_kit.dir.Dir`'s path-math methods, covering the
Windows-vs-Linux divergence fixed by switching from `pathlib.Path` to
`pathlib.PurePosixPath`: `Dir.path` is always a forward-slash-delimited
rclone remote path, never a local filesystem path, so parsing it with
`WindowsPath` semantics on Windows (which treats a literal `\\` as a
directory separator) silently corrupts any path segment containing one.
"""

from typing import cast

from rclone_kit.client import Rclone
from rclone_kit.dir import Dir
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath

_BACKSLASH_NAME = "weird" + chr(92) + "name.txt"
_FAKE_RCLONE = cast(Rclone, object())


def _remote() -> Remote:
    return Remote(name="remote", rclone=_FAKE_RCLONE)


def _dir(path: str, name: str, is_dir: bool = True) -> Dir:
    remote = _remote()
    rpath = RPath(
        remote=remote,
        path=path,
        name=name,
        size=0,
        mime_type="inode/directory" if is_dir else "text/plain",
        mod_time="",
        is_dir=is_dir,
    )
    rpath.set_rclone(_FAKE_RCLONE)
    return Dir(rpath)


def test_truediv_preserves_literal_backslash_in_joined_name() -> None:
    parent = _dir("Bucket/subdir", "subdir")

    child = parent / _BACKSLASH_NAME

    assert child.path.path == f"Bucket/subdir/{_BACKSLASH_NAME}"


def test_relative_to_preserves_literal_backslash_in_result() -> None:
    parent = _dir("Bucket/subdir", "subdir")
    child = _dir(f"Bucket/subdir/{_BACKSLASH_NAME}", _BACKSLASH_NAME, is_dir=False)

    assert child.relative_to(parent) == _BACKSLASH_NAME


def test_relative_to_ordinary_nested_path() -> None:
    parent = _dir("Bucket", "Bucket")
    child = _dir("Bucket/subdir/file.txt", "file.txt", is_dir=False)

    assert child.relative_to(parent) == "subdir/file.txt"
