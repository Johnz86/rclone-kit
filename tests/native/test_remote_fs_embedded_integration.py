"""Native-backed test for the direct-RC `RemoteFS` facade (ledger rows
F01-F05), per the Wave G design (`native_c_abi_wave_g_review_and_design.md`).

`RemoteFS` is backend-agnostic (it only depends on the `RemoteFSAccess`
protocol, which `Rclone` satisfies regardless of whether `src` addresses a
cloud remote or a local path), so these tests exercise it against local
temp directories, without needing live cloud credentials.

Skipped automatically when no built native library exists (run
`scripts/native/build.py` first).
"""

from pathlib import Path

import pytest
from conftest import NATIVE_LIBRARY_AVAILABLE

from rclone_kit.client import Rclone

pytestmark = pytest.mark.skipif(
    not NATIVE_LIBRARY_AVAILABLE,
    reason="No built native library found; run scripts/native/build.py first.",
)


def test_filesystem_constructs_without_starting_a_server(tmp_path: Path, embedded: Rclone) -> None:
    fs = embedded.filesystem(str(tmp_path))

    assert fs.server is None


def test_exists_for_present_and_missing_paths(tmp_path: Path, embedded: Rclone) -> None:
    (tmp_path / "hello.txt").write_bytes(b"hi")

    embedded_fs = embedded.filesystem(str(tmp_path))

    present = str(tmp_path / "hello.txt")
    missing = str(tmp_path / "nope.txt")

    assert embedded_fs.exists(present) is True
    assert embedded_fs.exists(missing) is False


def test_is_dir_and_is_file_distinguish_correctly(tmp_path: Path, embedded: Rclone) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "hello.txt").write_bytes(b"hi")

    fs = embedded.filesystem(str(tmp_path))

    assert fs.is_dir(str(tmp_path / "sub")) is True
    assert fs.is_file(str(tmp_path / "sub")) is False
    assert fs.is_dir(str(tmp_path / "hello.txt")) is False
    assert fs.is_file(str(tmp_path / "hello.txt")) is True


def test_is_dir_and_is_file_are_false_for_a_missing_path(tmp_path: Path, embedded: Rclone) -> None:
    fs = embedded.filesystem(str(tmp_path))
    missing = str(tmp_path / "does-not-exist")

    assert fs.is_dir(missing) is False
    assert fs.is_file(missing) is False


def test_ls_marks_directories_with_a_trailing_slash(tmp_path: Path, embedded: Rclone) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")

    embedded_fs = embedded.filesystem(str(tmp_path))

    embedded_files, embedded_dirs = embedded_fs.ls(str(tmp_path))

    assert sorted(embedded_files) == ["a.txt", "b.txt"]
    assert sorted(embedded_dirs) == ["sub/"]


def test_ls_raises_file_not_found_for_a_missing_path(tmp_path: Path, embedded: Rclone) -> None:
    fs = embedded.filesystem(str(tmp_path))
    missing = str(tmp_path / "does-not-exist")

    with pytest.raises(FileNotFoundError):
        fs.ls(missing)


def test_read_write_copy_remove_still_work_through_fspath(tmp_path: Path, embedded: Rclone) -> None:
    fs = embedded.filesystem(str(tmp_path))
    with fs.cwd() as cwd:
        target = cwd / "manifest.txt"
        target.write_text("hello from RemoteFS")

        assert target.exists() is True
        assert target.read_text() == "hello from RemoteFS"

        moved = cwd / "moved.txt"
        target.fs.copy(target.path, moved.path)
        target.remove()
        assert moved.exists() is True
        assert not target.exists()

        moved.remove()
        assert not moved.exists()


def test_dispose_is_a_no_op_when_no_server_was_ever_started(
    tmp_path: Path, embedded: Rclone
) -> None:
    fs = embedded.filesystem(str(tmp_path))

    fs.dispose()
    fs.dispose()  # idempotent

    assert fs.server is None
