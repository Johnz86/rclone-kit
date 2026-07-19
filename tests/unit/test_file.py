"""Unit tests for `rclone_kit.file`'s path-math methods, covering the
Windows-vs-Linux divergence fixed by switching from `pathlib.Path` to
`pathlib.PurePosixPath`: these operate on `remote:bucket/path`-style rclone
paths, never local filesystem paths, so parsing them with `WindowsPath`
semantics on Windows (which treats a literal `\\` as a directory separator)
silently corrupts any path segment containing one.
"""

from typing import cast

from rclone_kit.file import File, FileItem
from rclone_kit.rclone_impl import RcloneImpl
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath

_BACKSLASH_NAME = "weird" + chr(92) + "name.txt"
_FAKE_RCLONE = cast(RcloneImpl, object())


def _file(path: str, name: str) -> File:
    remote = Remote(name="remote", rclone=_FAKE_RCLONE)
    rpath = RPath(
        remote=remote,
        path=path,
        name=name,
        size=1,
        mime_type="text/plain",
        mod_time="",
        is_dir=False,
    )
    rpath.set_rclone(_FAKE_RCLONE)
    return File(rpath)


def test_relative_to_preserves_literal_backslash_in_result() -> None:
    f = _file(f"Bucket/subdir/{_BACKSLASH_NAME}", _BACKSLASH_NAME)

    assert f.relative_to("remote:Bucket/subdir") == _BACKSLASH_NAME


def test_relative_to_ordinary_nested_path() -> None:
    f = _file("Bucket/subdir/file.txt", "file.txt")

    assert f.relative_to("remote:Bucket") == "subdir/file.txt"


def test_file_item_from_json_preserves_literal_backslash_in_parent() -> None:
    item = FileItem.from_json(
        "remote",
        {
            "Path": f"Bucket/subdir/{_BACKSLASH_NAME}",
            "Name": _BACKSLASH_NAME,
            "Size": 1,
            "MimeType": "text/plain",
            "ModTime": "2024-01-01T00:00:00Z",
        },
    )

    assert item is not None
    assert item.parent == "Bucket/subdir"
    assert item.name == _BACKSLASH_NAME


def test_file_item_from_json_ordinary_nested_path() -> None:
    item = FileItem.from_json(
        "remote",
        {
            "Path": "Bucket/subdir/file.txt",
            "Name": "file.txt",
            "Size": 1,
            "MimeType": "text/plain",
            "ModTime": "2024-01-01T00:00:00Z",
        },
    )

    assert item is not None
    assert item.parent == "Bucket/subdir"
