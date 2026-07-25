"""Native-backed test for the embedded RC-backed listing/stat operations
(ledger rows M02, L05, L06/L07, L08, L10) and `config_show` (M04).

Uses local filesystem paths, so no configured remote is required; reuses
the shared, already-initialized `native_runtime` session fixture (see
`conftest.py`) rather than initializing its own.

Skipped automatically when no built native library exists (run
`scripts/native/build.py` first).
"""

from pathlib import Path

import pytest
from conftest import NATIVE_LIBRARY_AVAILABLE

from rclone_kit.client import Rclone
from rclone_kit.diff import DiffOption
from rclone_kit.types import ListingOption

pytestmark = pytest.mark.skipif(
    not NATIVE_LIBRARY_AVAILABLE,
    reason="No built native library found; run scripts/native/build.py first.",
)


def test_stat_returns_the_expected_file(tmp_path: Path, embedded: Rclone) -> None:
    (tmp_path / "hello.txt").write_bytes(b"hello world")
    src = f"{tmp_path}/hello.txt"

    embedded_file = embedded.stat(src)

    assert embedded_file.size == len(b"hello world")
    assert embedded_file.name == "hello.txt"
    assert not embedded_file.path.is_dir


def test_stat_raises_file_not_found_for_a_missing_path(tmp_path: Path, embedded: Rclone) -> None:
    src = f"{tmp_path}/does-not-exist.txt"

    with pytest.raises(FileNotFoundError):
        embedded.stat(src)


def test_modtime_is_transitively_embedded_through_stat(tmp_path: Path, embedded: Rclone) -> None:
    (tmp_path / "hello.txt").write_bytes(b"hello")
    src = f"{tmp_path}/hello.txt"

    # modtime()/modtime_dt() have no execution branch of their own; they
    # only work here because stat() dispatches to the embedded adapter.
    assert embedded.modtime(src) == embedded.stat(src).mod_time()
    assert embedded.modtime_dt(src) == embedded.stat(src).mod_time_dt()


def test_size_file_returns_the_expected_size(tmp_path: Path, embedded: Rclone) -> None:
    (tmp_path / "hello.txt").write_bytes(b"hello world!")
    src = f"{tmp_path}/hello.txt"

    assert embedded.size_file(src).as_int() == len(b"hello world!")


def test_size_files_returns_expected_sizes_for_a_batch_of_files(
    tmp_path: Path, embedded: Rclone
) -> None:
    (tmp_path / "a.txt").write_bytes(b"aaa")
    (tmp_path / "b.txt").write_bytes(b"bb")
    (tmp_path / "c.txt").write_bytes(b"ignored")
    src = str(tmp_path)

    embedded_result = embedded.size_files(src, ["a.txt", "b.txt"])

    assert embedded_result.total_size == 5
    assert embedded_result.file_sizes == {"a.txt": 3, "b.txt": 2}


def test_size_files_single_file_uses_the_size_file_shortcut(
    tmp_path: Path, embedded: Rclone
) -> None:
    (tmp_path / "solo.txt").write_bytes(b"twelve bytes")

    result = embedded.size_files(str(tmp_path), ["solo.txt"])

    assert result.total_size == len(b"twelve bytes")
    assert result.file_sizes == {"solo.txt": len(b"twelve bytes")}


def test_size_files_empty_list_returns_zero(tmp_path: Path, embedded: Rclone) -> None:
    result = embedded.size_files(str(tmp_path), [])

    assert result.total_size == 0
    assert result.file_sizes == {}


def test_exists_for_present_and_missing_paths(tmp_path: Path, embedded: Rclone) -> None:
    (tmp_path / "hello.txt").write_bytes(b"hello")
    present = f"{tmp_path}/hello.txt"
    missing = f"{tmp_path}/nope.txt"

    assert embedded.exists(present) is True
    assert embedded.exists(missing) is False


def test_listremotes_returns_a_list_of_remote_when_none_configured(
    embedded: Rclone,
) -> None:
    assert embedded.listremotes() == []


def test_config_paths_returns_a_config_cache_temp_triple(embedded: Rclone) -> None:
    paths = embedded.config_paths()

    assert len(paths) == 3
    assert all(isinstance(p, Path) for p in paths)


def test_config_show_whole_config_returns_text(embedded: Rclone) -> None:
    assert isinstance(embedded.config_show(), str)


def test_native_build_info_reports_a_real_build(embedded: Rclone) -> None:
    # Wave I design, C02: the embedded replacement for upgrade_rclone()'s
    # old "which rclone am I running" concern.
    info = embedded.native_build_info()

    assert info.abi_version >= 1
    assert info.rclone_version
    assert info.go_version


def _make_tree(root: Path) -> None:
    (root / "a.txt").write_bytes(b"a")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_bytes(b"bb")
    (root / "sub" / "deeper").mkdir()
    (root / "sub" / "deeper" / "c.txt").write_bytes(b"ccc")


def test_ls_non_recursive(tmp_path: Path, embedded: Rclone) -> None:
    _make_tree(tmp_path)
    src = str(tmp_path)

    embedded_listing = embedded.ls(src)
    embedded_names = sorted(f.name for f in embedded_listing.files) + sorted(
        d.name for d in embedded_listing.dirs
    )

    assert embedded_names == ["a.txt", "sub"]


def test_ls_unlimited_recursion(tmp_path: Path, embedded: Rclone) -> None:
    _make_tree(tmp_path)
    src = str(tmp_path)

    embedded_paths = sorted(str(f.path) for f in embedded.ls(src, max_depth=-1).files)

    assert len(embedded_paths) == 3


def test_ls_bounded_recursion(tmp_path: Path, embedded: Rclone) -> None:
    _make_tree(tmp_path)
    src = str(tmp_path)

    embedded_listing = embedded.ls(src, max_depth=2)

    embedded_names = sorted(f.name for f in embedded_listing.files) + sorted(
        d.name for d in embedded_listing.dirs
    )
    assert embedded_names == ["a.txt", "b.txt", "deeper", "sub"]


def test_ls_files_only(tmp_path: Path, embedded: Rclone) -> None:
    _make_tree(tmp_path)
    src = str(tmp_path)

    embedded_listing = embedded.ls(src, max_depth=-1, listing_option=ListingOption.FILES_ONLY)

    assert embedded_listing.dirs == []
    assert sorted(f.name for f in embedded_listing.files) == ["a.txt", "b.txt", "c.txt"]


def test_ls_reads_file_sizes_correctly(tmp_path: Path, embedded: Rclone) -> None:
    _make_tree(tmp_path)

    listing = embedded.ls(str(tmp_path))

    sizes = {f.name: f.size for f in listing.files}
    assert sizes == {"a.txt": 1}


def test_is_synced_true_for_identical_directories(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_bytes(b"hello")
    (dst / "a.txt").write_bytes(b"hello")

    assert embedded.is_synced(str(src), str(dst)) is True


def test_is_synced_false_for_differing_directories(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_bytes(b"hello")
    (dst / "a.txt").write_bytes(b"different content")

    assert embedded.is_synced(str(src), str(dst)) is False


def _make_diff_tree(root: Path) -> tuple[Path, Path]:
    src = root / "src"
    dst = root / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "same.txt").write_bytes(b"same content")
    (dst / "same.txt").write_bytes(b"same content")
    (src / "only_src.txt").write_bytes(b"only in src")
    (dst / "only_dst.txt").write_bytes(b"only in dst")
    (src / "differs.txt").write_bytes(b"version A")
    (dst / "differs.txt").write_bytes(b"version B, a different length")
    return src, dst


def test_diff_combined(tmp_path: Path, embedded: Rclone) -> None:
    src, dst = _make_diff_tree(tmp_path)

    embedded_items = {
        (i.type, i.path) for i in embedded.diff(str(src), str(dst), diff_option=DiffOption.COMBINED)
    }

    assert len(embedded_items) == 4


def test_diff_missing_on_dst(tmp_path: Path, embedded: Rclone) -> None:
    src, dst = _make_diff_tree(tmp_path)

    embedded_paths = {
        i.path for i in embedded.diff(str(src), str(dst), diff_option=DiffOption.MISSING_ON_DST)
    }

    assert embedded_paths == {"only_src.txt"}


def test_diff_differ_and_match(tmp_path: Path, embedded: Rclone) -> None:
    src, dst = _make_diff_tree(tmp_path)

    differ_paths = {
        i.path for i in embedded.diff(str(src), str(dst), diff_option=DiffOption.DIFFER)
    }
    match_paths = {i.path for i in embedded.diff(str(src), str(dst), diff_option=DiffOption.MATCH)}

    assert differ_paths == {"differs.txt"}
    assert match_paths == {"same.txt"}
