"""Native-backed test for the embedded bounded-memory listing stream
(ledger row L02 `ls_stream`, L03 `save_to_db` transitively), per the Wave F
design (`native_c_abi_wave_f_review_and_design.md`).

Skipped automatically when no built native library exists (run
`scripts/native/build.py` first).
"""

from pathlib import Path

import pytest
from conftest import NATIVE_LIBRARY_AVAILABLE

from rclone_kit.client import Rclone
from rclone_kit.exceptions import RcloneCommandError

pytestmark = pytest.mark.skipif(
    not NATIVE_LIBRARY_AVAILABLE,
    reason="No built native library found; run scripts/native/build.py first.",
)


def test_ls_stream_file_count(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    src.mkdir()
    for i in range(5):
        (src / f"f{i}.txt").write_bytes(b"x")

    with embedded.ls_stream(str(src)) as embedded_stream:
        embedded_names = sorted(item.name for item in embedded_stream.files())

    assert embedded_names == [f"f{i}.txt" for i in range(5)]


def test_ls_stream_recurses_into_subdirectories(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "top.txt").write_bytes(b"x")
    (src / "sub" / "nested.txt").write_bytes(b"y")

    with embedded.ls_stream(str(src), max_depth=-1) as stream:
        names = sorted(item.name for item in stream.files())

    assert names == ["nested.txt", "top.txt"]


def test_ls_stream_non_recursive_only_lists_top_level(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "top.txt").write_bytes(b"x")
    (src / "sub" / "nested.txt").write_bytes(b"y")

    with embedded.ls_stream(str(src), max_depth=0) as stream:
        names = [item.name for item in stream.files()]

    assert names == ["top.txt"]


def test_ls_stream_handles_a_batch_larger_than_one_internal_page(
    tmp_path: Path, embedded: Rclone
) -> None:
    # Exercise pulling across multiple rclonekit/liststream/next batches,
    # not just a single-page listing.
    src = tmp_path / "src"
    src.mkdir()
    for i in range(2500):
        (src / f"f{i:05d}.txt").write_bytes(b"x")

    with embedded.ls_stream(str(src)) as stream:
        count = sum(1 for _ in stream.files())

    assert count == 2500


def test_ls_stream_empty_directory_yields_nothing(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    src.mkdir()

    with embedded.ls_stream(str(src)) as stream:
        items = list(stream.files())

    assert items == []


def test_ls_stream_files_paged_batches_correctly(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    src.mkdir()
    for i in range(25):
        (src / f"f{i:03d}.txt").write_bytes(b"x")

    with embedded.ls_stream(str(src)) as stream:
        pages = list(stream.files_paged(page_size=10))

    assert [len(page) for page in pages] == [10, 10, 5]


def test_ls_stream_close_removes_it_from_the_client_tracking_set(
    tmp_path: Path, embedded: Rclone
) -> None:
    # A disposed stream must not stay tracked forever - see finding #5's
    # "EmbeddedFilesStream instances aren't tracked by close()", now fixed
    # by tracking them the same way ServeHandle/MountHandle are.
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_bytes(b"x")

    stream = embedded.ls_stream(str(src))
    assert stream in embedded._file_streams

    stream.close()

    assert stream not in embedded._file_streams


def test_ls_stream_can_be_closed_early_without_hanging(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    src.mkdir()
    for i in range(100):
        (src / f"f{i:03d}.txt").write_bytes(b"x")

    with embedded.ls_stream(str(src)) as stream:
        gen = stream.files()
        first = next(gen)
        assert first.name == "f000.txt"
    # Exiting the `with` block above closed the stream early - no hang, no
    # error, and the runtime is left usable for a subsequent call.
    assert embedded.exists(str(src / "f000.txt")) is True


def test_save_to_db_matches_ls_stream_transitively(tmp_path: Path, embedded: Rclone) -> None:
    pytest.importorskip("sqlmodel")
    import sqlite3

    from rclone_kit.db.db import _to_table_name

    src = tmp_path / "src"
    src.mkdir()
    for i in range(5):
        (src / f"f{i}.txt").write_bytes(b"x")

    db_path = tmp_path / "out.db"
    embedded.save_to_db(str(src), f"sqlite:///{db_path}")

    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    try:
        # `DB.__init__` creates every table dynamically registered against
        # SQLModel's process-wide metadata so far (not just this db's own),
        # so a fresh db file can contain other tests' leftover table
        # definitions too - a separate, pre-existing quirk of `rclone_kit.
        # db`, not this test's concern. Only this test's own table matters
        # here.
        table_name = _to_table_name(str(src))
        query = f'select count(*) from "{table_name}"'  # noqa: S608
        (count,) = conn.execute(query).fetchone()
        assert count == 5
    finally:
        conn.close()


def test_ls_stream_missing_source_raises_rclone_command_error(
    tmp_path: Path, embedded: Rclone
) -> None:
    missing = tmp_path / "does-not-exist"

    with pytest.raises(RcloneCommandError), embedded.ls_stream(str(missing)) as stream:
        list(stream.files())
