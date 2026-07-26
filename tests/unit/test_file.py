"""Unit tests for `rclone_kit.file`'s path-math methods, covering the
Windows-vs-Linux divergence fixed by switching from `pathlib.Path` to
`pathlib.PurePosixPath`: these operate on `remote:bucket/path`-style rclone
paths, never local filesystem paths, so parsing them with `WindowsPath`
semantics on Windows (which treats a literal `\\` as a directory separator)
silently corrupts any path segment containing one.
"""

from typing import cast

from rclone_kit.client import Rclone
from rclone_kit.file import File, FileItem
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath

_BACKSLASH_NAME = "weird" + chr(92) + "name.txt"
_FAKE_RCLONE = cast(Rclone, object())


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


def test_to_string_on_a_local_path_has_no_leading_colon() -> None:
    """A local File (no remote at all - see rc.paths.split_remote_and_path)
    has nothing to prefix; the previous `f"{remote.name}:{path}"`
    reconstruction produced a leading ":" (e.g. ":/tmp/a.txt") that
    doesn't round-trip back through `RcPath.parse`.
    """
    remote = Remote(name="", rclone=_FAKE_RCLONE)
    rpath = RPath(
        remote=remote,
        path="/srv/data/a.txt",
        name="a.txt",
        size=1,
        mime_type="text/plain",
        mod_time="",
        is_dir=False,
    )
    rpath.set_rclone(_FAKE_RCLONE)
    f = File(rpath)

    assert f.to_string(include_remote=True) == "/srv/data/a.txt"


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


class _RecordingRclone:
    """Records the `src` passed to `read_text`; ledger row F06 requires
    `File.read_text` to delegate here rather than call `_run(["cat", ...])`
    directly.
    """

    def __init__(self) -> None:
        self.read_text_calls: list[str] = []

    def read_text(self, src: str) -> str:
        self.read_text_calls.append(src)
        return "file contents"

    def _run(self, *_args: object, **_kwargs: object):
        raise AssertionError("File.read_text must not call _run directly")


def test_file_read_text_delegates_to_associated_rclone() -> None:
    recording_rclone = _RecordingRclone()
    remote = Remote(name="remote", rclone=cast(Rclone, recording_rclone))
    rpath = RPath(
        remote=remote,
        path="Bucket/file.txt",
        name="file.txt",
        size=1,
        mime_type="text/plain",
        mod_time="",
        is_dir=False,
    )
    rpath.set_rclone(cast(Rclone, recording_rclone))
    f = File(rpath)

    result = f.read_text()

    assert result == "file contents"
    assert recording_rclone.read_text_calls == ["remote:Bucket/file.txt"]
