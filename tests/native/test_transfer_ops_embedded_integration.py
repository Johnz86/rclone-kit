"""Native-backed parity check for the embedded RC-backed transfer
operations `copy_to`/`purge`/`cleanup`, plus verification that
`read_bytes()`/`read_text()` work transitively - both only ever call
`self.copy_to`, so they need no embedded adapter of their own.

All three route through the shared async job engine (`_JobMonitor`), so a
`copy_to(check=True)` failure raises `OperationFailedError` - not a raw
`RcCallError` - the same execution-independent exception
`read_bytes()`/`read_text()` translate to `RcloneCommandError`.

Also covers `write_bytes()`/`write_text()` (transitively embedded through
`copy_to()`) and `copy_bytes()` (backed by the downstream
`rclonekit/readrange` RC method).

Skipped automatically when no built native library exists (run
`scripts/native/build.py` first).
"""

from pathlib import Path

import pytest
from conftest import NATIVE_LIBRARY_AVAILABLE

from rclone_kit.client import Rclone
from rclone_kit.exceptions import (
    OperationFailedError,
    RcloneCommandError,
)

pytestmark = pytest.mark.skipif(
    not NATIVE_LIBRARY_AVAILABLE,
    reason="No built native library found; run scripts/native/build.py first.",
)


def test_copy_to_for_a_real_file(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src.txt"
    src.write_bytes(b"hello world")
    embedded_dst = tmp_path / "embedded_dst.txt"

    embedded.copy_to(str(src), str(embedded_dst))

    assert embedded_dst.read_bytes() == b"hello world"


def test_copy_to_works_with_bare_relative_basenames(
    tmp_path: Path, embedded: Rclone, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bare relative filename with no directory separator ("src.txt") must
    # resolve its parent to the current directory, not raise.
    monkeypatch.chdir(tmp_path)
    Path("src.txt").write_bytes(b"relative basename works")

    embedded.copy_to("src.txt", "dst.txt")

    assert Path("dst.txt").read_bytes() == b"relative basename works"


def test_copy_to_relative_basename_destination_also_works(
    tmp_path: Path, embedded: Rclone, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The destination side of the same rule: a bare relative basename with
    # no existing file yet at that name must still resolve its parent to ".".
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "abs_src.txt"
    src.write_bytes(b"dest side relative basename")

    embedded.copy_to(str(src), "relative_dst.txt")

    assert Path("relative_dst.txt").read_bytes() == b"dest side relative basename"


def test_copy_to_raises_operation_failed_error_by_default_on_missing_source(
    tmp_path: Path, embedded: Rclone
) -> None:
    missing = tmp_path / "does-not-exist.txt"
    dst = tmp_path / "dst.txt"

    with pytest.raises(OperationFailedError):
        embedded.copy_to(str(missing), str(dst))


def test_copy_to_leaves_no_partial_destination_file_on_failure(
    tmp_path: Path, embedded: Rclone
) -> None:
    missing = tmp_path / "does-not-exist.txt"
    dst = tmp_path / "dst.txt"

    result = embedded.copy_to(str(missing), str(dst), check=False)

    assert result.ok is False
    assert not dst.exists()


def test_read_bytes_and_read_text_work_transitively_through_copy_to(
    tmp_path: Path, embedded: Rclone
) -> None:
    src = tmp_path / "hello.txt"
    src.write_text("hello, rclone-kit", encoding="utf-8")

    assert embedded.read_bytes(str(src)) == b"hello, rclone-kit"
    assert embedded.read_text(str(src)) == "hello, rclone-kit"


def test_write_bytes_transitively_through_copy_to(tmp_path: Path, embedded: Rclone) -> None:
    # write_bytes()/write_text() need no embedded-specific code of their
    # own - they already call copy_to(), confirmed empirically here rather
    # than just inferred from reading the dispatch.
    embedded_dst = tmp_path / "embedded_out.bin"

    embedded.write_bytes(b"hello embedded write_bytes", str(embedded_dst))

    assert embedded_dst.read_bytes() == b"hello embedded write_bytes"


def test_write_text_transitively_through_write_bytes(tmp_path: Path, embedded: Rclone) -> None:
    embedded_dst = tmp_path / "embedded_out.txt"

    embedded.write_text("hello embedded write_text", str(embedded_dst))

    assert embedded_dst.read_text(encoding="utf-8") == "hello embedded write_text"


def test_print_works_transitively_through_read_text(
    tmp_path: Path, embedded: Rclone, capsys: pytest.CaptureFixture[str]
) -> None:
    # print() only ever calls read_text(), already embedded.
    src = tmp_path / "hello.txt"
    src.write_text("printed via embedded read_text", encoding="utf-8")

    embedded.print(str(src))

    assert capsys.readouterr().out.strip() == "printed via embedded read_text"


def test_read_bytes_raises_rclone_command_error_for_a_missing_source(
    tmp_path: Path, embedded: Rclone
) -> None:
    # read_bytes() must always raise RcloneCommandError for a missing
    # source, not a bare RcCallError/OperationFailedError leaking through.
    missing = tmp_path / "does-not-exist.txt"

    with pytest.raises(RcloneCommandError):
        embedded.read_bytes(str(missing))


def test_purge_for_a_real_directory(tmp_path: Path, embedded: Rclone) -> None:
    embedded_target = tmp_path / "embedded_target"
    embedded_target.mkdir()
    (embedded_target / "sub").mkdir()
    (embedded_target / "sub" / "a.txt").write_bytes(b"x")

    embedded_result = embedded.purge(str(embedded_target))

    assert embedded_result.ok is True
    assert not embedded_target.exists()


def test_purge_never_raises_for_a_missing_directory(tmp_path: Path, embedded: Rclone) -> None:
    missing = tmp_path / "does-not-exist"

    result = embedded.purge(str(missing))

    assert result.ok is False


def test_cleanup_never_raises_for_a_local_path(tmp_path: Path, embedded: Rclone) -> None:
    # The local backend doesn't support cleanup; it should report failure
    # by returning a non-ok result, not by raising.
    embedded_result = embedded.cleanup(str(tmp_path))

    assert embedded_result.ok is False


def test_copy_bytes_for_an_exact_range(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src.bin"
    content = bytes(range(256)) * 4
    src.write_bytes(content)
    embedded_out = tmp_path / "embedded_out.bin"

    embedded.copy_bytes(str(src), offset=10, length=20, outfile=embedded_out)

    assert embedded_out.read_bytes() == content[10:30]


def test_copy_bytes_zero_length_produces_an_empty_file(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello world")
    out = tmp_path / "out.bin"

    embedded.copy_bytes(str(src), offset=5, length=0, outfile=out)

    assert out.read_bytes() == b""


def test_copy_bytes_range_extending_past_eof_copies_what_is_available(
    tmp_path: Path, embedded: Rclone
) -> None:
    src = tmp_path / "src.bin"
    content = bytes(range(256)) * 4  # 1024 bytes
    src.write_bytes(content)
    out = tmp_path / "out.bin"

    embedded.copy_bytes(str(src), offset=1000, length=100, outfile=out)

    assert out.read_bytes() == content[1000:1024]


def test_copy_bytes_offset_at_eof_produces_an_empty_file(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"1234567890")
    out = tmp_path / "out.bin"

    embedded.copy_bytes(str(src), offset=10, length=10, outfile=out)

    assert out.read_bytes() == b""


def test_copy_bytes_full_file_range(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src.bin"
    content = b"the quick brown fox"
    src.write_bytes(content)
    out = tmp_path / "out.bin"

    embedded.copy_bytes(str(src), offset=0, length=len(content), outfile=out)

    assert out.read_bytes() == content


def test_copy_bytes_missing_source_raises_rclone_command_error(
    tmp_path: Path, embedded: Rclone
) -> None:
    missing = tmp_path / "does-not-exist.bin"
    out = tmp_path / "out.bin"

    with pytest.raises(RcloneCommandError):
        embedded.copy_bytes(str(missing), offset=0, length=10, outfile=out)
