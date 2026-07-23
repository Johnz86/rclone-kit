"""Native-backed parity check that `walk()` (ledger row L13) and
`scan_missing_folders()` (L14) work correctly under `execution="embedded"`
with no code changes of their own - both only ever call `Dir.ls()`, which
already dispatches to the embedded `operations/list` adapter once L01
landed.

Skipped automatically when no built native target exists (run
`scripts/native/build.py` first).
"""

from pathlib import Path

import pytest

from conftest import NATIVE_EXECUTABLE_AVAILABLE
from rclone_kit.client import Rclone

pytestmark = pytest.mark.skipif(
    not NATIVE_EXECUTABLE_AVAILABLE,
    reason="No built native executable found; run scripts/native/build.py first.",
)


def _make_tree(root: Path) -> None:
    (root / "a.txt").write_bytes(b"a")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_bytes(b"bb")
    (root / "sub" / "deeper").mkdir()
    (root / "sub" / "deeper" / "c.txt").write_bytes(b"ccc")


def test_walk_matches_cli_over_a_nested_tree(tmp_path: Path, embedded: Rclone, cli: Rclone) -> None:
    _make_tree(tmp_path)
    src = str(tmp_path)

    embedded_files = sorted(
        f.name for listing in embedded.walk(src) for f in listing.files
    )
    cli_files = sorted(f.name for listing in cli.walk(src) for f in listing.files)

    assert embedded_files == cli_files == ["a.txt", "b.txt", "c.txt"]


def test_walk_breadth_vs_depth_first_match_cli(
    tmp_path: Path, embedded: Rclone, cli: Rclone
) -> None:
    _make_tree(tmp_path)
    src = str(tmp_path)

    for breadth_first in (True, False):
        embedded_files = sorted(
            f.name
            for listing in embedded.walk(src, breadth_first=breadth_first)
            for f in listing.files
        )
        cli_files = sorted(
            f.name
            for listing in cli.walk(src, breadth_first=breadth_first)
            for f in listing.files
        )
        assert embedded_files == cli_files


def test_scan_missing_folders_matches_cli(
    tmp_path: Path, embedded: Rclone, cli: Rclone
) -> None:
    src_root = tmp_path / "src"
    dst_root = tmp_path / "dst"
    src_root.mkdir()
    dst_root.mkdir()
    (src_root / "present").mkdir()
    (src_root / "present" / "child.txt").write_bytes(b"x")
    (src_root / "missing").mkdir()
    (src_root / "missing" / "nested").mkdir()
    (dst_root / "present").mkdir()

    embedded_missing = sorted(
        d.to_string(include_remote=False)
        for d in embedded.scan_missing_folders(str(src_root), str(dst_root))
    )
    cli_missing = sorted(
        d.to_string(include_remote=False)
        for d in cli.scan_missing_folders(str(src_root), str(dst_root))
    )

    assert embedded_missing == cli_missing
    assert any(name.endswith("missing") for name in embedded_missing)
