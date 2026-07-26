"""Unit tests for `rclone_kit.rc.paths.RcPath`.

Every bare local reference (no remote prefix, not a Windows drive-prefixed
absolute path) is absolutized against the current working directory as
soon as it is parsed - see `RcPath`'s module docstring and `_resolve_local`
for why. These tests compute the expected absolutized form with
`Path(...).resolve()` directly, the same primitive the implementation
uses, rather than hardcoding a platform-specific string.
"""

import os
import sys
from pathlib import Path

import pytest

from rclone_kit.rc.paths import RcPath, RcPathParts, split_remote_and_path


def _abs(path: str) -> str:
    return str(Path(path).resolve())


@pytest.mark.parametrize(
    ("raw", "expected_fs", "expected_remote"),
    [
        ("remote:path/to/object", "remote:", "path/to/object"),
        ("remote:", "remote:", ""),
        ("remote:path/", "remote:", "path"),
        ("remote:/path", "remote:", "path"),
        ("remote:文件/résumé.txt", "remote:", "文件/résumé.txt"),
        ("C:\\Users\\example\\file.txt", "C:\\Users\\example\\file.txt", ""),
        ("C:/Users/example/file.txt", "C:/Users/example/file.txt", ""),
        ("C:", "C:", ""),
    ],
)
def test_parse_splits_fs_and_remote(raw: str, expected_fs: str, expected_remote: str) -> None:
    parsed = RcPath.parse(raw)

    assert parsed.fs == expected_fs
    assert parsed.remote == expected_remote


@pytest.mark.parametrize("raw", ["/home/user/file.txt", "relative/local/path"])
def test_parse_absolutizes_a_bare_local_reference(raw: str) -> None:
    parsed = RcPath.parse(raw)

    assert parsed.fs == _abs(raw)
    assert parsed.remote == ""


def test_parse_does_not_treat_multi_letter_prefix_as_a_drive() -> None:
    parsed = RcPath.parse("gdrive:some/file.txt")

    assert parsed.fs == "gdrive:"
    assert parsed.remote == "some/file.txt"


def test_str_reconstructs_the_original_shape() -> None:
    assert str(RcPath.parse("remote:path/to/object")) == "remote:path/to/object"
    assert str(RcPath.parse("/home/user/file.txt")) == _abs("/home/user/file.txt")


def test_as_parent_and_name_splits_remote_path_at_final_component() -> None:
    parsed = RcPath.parse("remote:path/to/object.txt")

    split = parsed.as_parent_and_name()

    assert split.fs == "remote:path/to"
    assert split.remote == "object.txt"


def test_as_parent_and_name_handles_single_component_remote() -> None:
    parsed = RcPath.parse("remote:object.txt")

    split = parsed.as_parent_and_name()

    assert split.fs == "remote:"
    assert split.remote == "object.txt"


def test_as_parent_and_name_rejects_bare_root() -> None:
    parsed = RcPath.parse("remote:")

    with pytest.raises(ValueError, match="no path component"):
        parsed.as_parent_and_name()


@pytest.mark.parametrize(
    ("raw", "expected_fs", "expected_remote"),
    [
        ("C:\\Users\\example\\file.txt", "C:\\Users\\example\\", "file.txt"),
        ("C:/Users/example/file.txt", "C:/Users/example/", "file.txt"),
    ],
)
def test_as_parent_and_name_splits_local_paths_too(
    raw: str, expected_fs: str, expected_remote: str
) -> None:
    """A local target's own full path is never a valid `fs` value for a
    single-target RC call (`operations/stat` rejects a bare file as `fs`
    with "is a file not a directory") - `as_parent_and_name` must split a
    local path exactly like a remote one.
    """
    split = RcPath.parse(raw).as_parent_and_name()

    assert split.fs == expected_fs
    assert split.remote == expected_remote


@pytest.mark.parametrize(
    "raw",
    [
        "/home/user/file.txt",
        "relative/local/path.txt",
        "/home/user/",
        "relative/local/dir/",
    ],
)
def test_as_parent_and_name_splits_an_absolutized_local_path(raw: str) -> None:
    split = RcPath.parse(raw).as_parent_and_name()

    absolute = Path(_abs(raw))
    assert split.fs == str(absolute.parent) + os.sep
    assert split.remote == absolute.name


@pytest.mark.parametrize("raw", ["C:", "/"])
def test_as_parent_and_name_rejects_bare_local_root(raw: str) -> None:
    parsed = RcPath.parse(raw)

    with pytest.raises(ValueError, match="no path component"):
        parsed.as_parent_and_name()


def test_as_parent_and_name_splits_a_bare_relative_basename() -> None:
    # The CLI accepts a bare relative filename and resolves its parent as
    # the current directory; as_parent_and_name must not be stricter. The
    # resolved parent is an absolute path, not a literal "." - see
    # `_resolve_local`.
    split = RcPath.parse("file.txt").as_parent_and_name()

    assert split.fs == _abs(".") + os.sep
    assert split.remote == "file.txt"


def test_as_parent_and_name_splits_a_bare_relative_directory_reference() -> None:
    split = RcPath.parse("foo/").as_parent_and_name()

    assert split.fs == _abs(".") + os.sep
    assert split.remote == "foo"


def test_as_parent_and_name_strips_trailing_separator_before_splitting() -> None:
    split = RcPath.parse("C:\\Users\\example\\").as_parent_and_name()

    assert split.fs == "C:\\Users\\"
    assert split.remote == "example"


def test_parse_splits_an_inline_remote_at_its_second_colon() -> None:
    parsed = RcPath.parse(":s3,provider=AWS:mybucket/prefix")

    assert parsed.fs == ":s3,provider=AWS:"
    assert parsed.remote == "mybucket/prefix"


def test_as_parent_and_name_splits_an_inline_remote() -> None:
    split = RcPath.parse(":s3,provider=AWS:mybucket/object.txt").as_parent_and_name()

    assert split.fs == ":s3,provider=AWS:mybucket"
    assert split.remote == "object.txt"


def test_parse_treats_a_bare_inline_remote_with_no_second_colon_as_local() -> None:
    # Malformed/incomplete inline-remote syntax has no path component to
    # split off; parse() must not raise, just decline to find a remote root.
    parsed = RcPath.parse(":not-a-complete-inline-remote")

    assert parsed.fs == ":not-a-complete-inline-remote"
    assert parsed.remote == ""


@pytest.mark.skipif(sys.platform != "win32", reason="UNC path syntax is a Windows-only concept")
def test_parse_and_split_handle_a_unc_path() -> None:
    parsed = RcPath.parse("\\\\server\\share\\dir\\file.txt")

    assert parsed.fs == "\\\\server\\share\\dir\\file.txt"
    assert parsed.remote == ""

    split = parsed.as_parent_and_name()
    assert split.fs == "\\\\server\\share\\dir\\"
    assert split.remote == "file.txt"


def test_remote_object_name_containing_a_literal_backslash_is_not_split_on_it() -> None:
    # Only "/" is a path separator for a remote target; a literal backslash
    # inside an object name must stay part of that single component.
    parsed = RcPath.parse("remote:folder/name\\with\\backslashes.txt")

    split = parsed.as_parent_and_name()

    assert split.fs == "remote:folder"
    assert split.remote == "name\\with\\backslashes.txt"


@pytest.mark.parametrize(
    ("raw", "expected_remote_name", "expected_rest"),
    [
        ("remote:path/to/object", "remote", "path/to/object"),
        ("remote:/path", "remote", "path"),
        ("/home/user/file.txt", "", "/home/user/file.txt"),
        ("relative/local/path.txt", "", "relative/local/path.txt"),
        ("C:\\Users\\example\\file.txt", "", "C:\\Users\\example\\file.txt"),
    ],
)
def test_split_remote_and_path_matches_parse(
    raw: str, expected_remote_name: str, expected_rest: str
) -> None:
    remote_name, rest = split_remote_and_path(raw)

    assert remote_name == expected_remote_name
    assert rest == expected_rest


def test_split_remote_and_path_does_not_resolve_a_bare_local_reference() -> None:
    # Unlike RcPath.parse, this must not absolutize - callers that only
    # need the (remote_name, remainder) split want the original string
    # back untouched.
    remote_name, rest = split_remote_and_path("relative/local/path.txt")

    assert remote_name == ""
    assert rest == "relative/local/path.txt"


def test_parse_parts_decomposes_a_real_remote_path() -> None:
    parts = RcPath.parse_parts("dst:TorrentBooks/libgenrs_nonfiction/204000/manifest.txt")

    assert parts == RcPathParts(
        remote="dst",
        parents=["TorrentBooks", "libgenrs_nonfiction", "204000"],
        name="manifest.txt",
    )


def test_parse_parts_decomposes_a_posix_local_path() -> None:
    parts = RcPath.parse_parts("/srv/data/subdir/manifest.txt")

    assert parts == RcPathParts(remote="", parents=["srv", "data", "subdir"], name="manifest.txt")


def test_parse_parts_decomposes_a_windows_drive_local_path() -> None:
    """The regression case for the Windows local-path grouping bug:
    `parse_file`/`group_files` previously collapsed everything past the
    drive letter into one opaque name because they only ever split on
    "/". `PureWindowsPath` makes this deterministic on any host OS, not
    only when actually running on Windows - see `RcPath.parse_parts`.
    """
    parts = RcPath.parse_parts(r"C:\Users\jan\data\subdir\manifest.txt")

    assert parts == RcPathParts(
        remote="C", parents=["Users", "jan", "data", "subdir"], name="manifest.txt"
    )


def test_parse_parts_decomposes_a_windows_drive_path_with_forward_slashes() -> None:
    parts = RcPath.parse_parts("C:/Users/jan/data/subdir/manifest.txt")

    assert parts == RcPathParts(
        remote="C", parents=["Users", "jan", "data", "subdir"], name="manifest.txt"
    )


def test_parse_parts_decomposes_a_bare_filename() -> None:
    parts = RcPath.parse_parts("manifest.txt")

    assert parts == RcPathParts(remote="", parents=[], name="manifest.txt")
