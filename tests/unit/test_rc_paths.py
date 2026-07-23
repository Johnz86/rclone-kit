"""Unit tests for `rclone_kit.rc.paths.RcPath`."""

import pytest

from rclone_kit.rc.paths import RcPath


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
        ("/home/user/file.txt", "/home/user/file.txt", ""),
        ("relative/local/path", "relative/local/path", ""),
    ],
)
def test_parse_splits_fs_and_remote(raw: str, expected_fs: str, expected_remote: str) -> None:
    parsed = RcPath.parse(raw)

    assert parsed.fs == expected_fs
    assert parsed.remote == expected_remote


def test_parse_does_not_treat_multi_letter_prefix_as_a_drive() -> None:
    parsed = RcPath.parse("gdrive:some/file.txt")

    assert parsed.fs == "gdrive:"
    assert parsed.remote == "some/file.txt"


def test_str_reconstructs_the_original_shape() -> None:
    assert str(RcPath.parse("remote:path/to/object")) == "remote:path/to/object"
    assert str(RcPath.parse("/home/user/file.txt")) == "/home/user/file.txt"


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
        ("/home/user/file.txt", "/home/user/", "file.txt"),
        ("relative/local/path.txt", "relative/local/", "path.txt"),
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


@pytest.mark.parametrize("raw", ["C:", "/"])
def test_as_parent_and_name_rejects_bare_local_root(raw: str) -> None:
    parsed = RcPath.parse(raw)

    with pytest.raises(ValueError, match="no path component"):
        parsed.as_parent_and_name()
