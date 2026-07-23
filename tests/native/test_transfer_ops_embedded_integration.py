"""Native-backed parity check for the embedded RC-backed transfer
operations (ledger rows T01, T02, T07), plus verification that
`read_bytes()`/`read_text()` (T11/T12) work transitively - both only ever
call `self.copy_to`, so they needed no embedded adapter of their own once
T02 landed.

Since Wave D Phase D7, all three route through the shared async job engine
(`_JobMonitor`), so a `copy_to(check=True)` failure raises
`OperationFailedError` - not a raw `RcCallError` - the same
execution-independent exception `read_bytes()`/`read_text()` translate to
`RcloneCommandError`, closing design-review finding F4.

Skipped automatically when no built native target exists (run
`scripts/native/build.py` first).
"""

from pathlib import Path

import pytest

from conftest import NATIVE_EXECUTABLE_AVAILABLE
from rclone_kit.client import Rclone
from rclone_kit.exceptions import OperationFailedError, RcloneCommandError

pytestmark = pytest.mark.skipif(
    not NATIVE_EXECUTABLE_AVAILABLE,
    reason="No built native executable found; run scripts/native/build.py first.",
)


def test_copy_to_matches_cli_for_a_real_file(tmp_path: Path, embedded: Rclone, cli: Rclone) -> None:
    src = tmp_path / "src.txt"
    src.write_bytes(b"hello world")
    embedded_dst = tmp_path / "embedded_dst.txt"
    cli_dst = tmp_path / "cli_dst.txt"

    embedded.copy_to(str(src), str(embedded_dst))
    cli.copy_to(str(src), str(cli_dst))

    assert embedded_dst.read_bytes() == cli_dst.read_bytes() == b"hello world"


def test_copy_to_works_with_bare_relative_basenames(
    tmp_path: Path, embedded: Rclone, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression test for design-review finding F8: RcPath.as_parent_and_name()
    # used to reject a bare relative filename with no directory separator
    # ("src.txt") instead of resolving its parent as the current directory,
    # which is what the CLI does.
    monkeypatch.chdir(tmp_path)
    Path("src.txt").write_bytes(b"relative basename works")

    embedded.copy_to("src.txt", "dst.txt")

    assert Path("dst.txt").read_bytes() == b"relative basename works"


def test_copy_to_relative_basename_destination_also_works(
    tmp_path: Path, embedded: Rclone, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F8 covers the destination side too: a bare relative basename with no
    # existing file yet at that name must still resolve its parent to ".".
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


def test_read_bytes_raises_rclone_command_error_for_a_missing_source(
    tmp_path: Path, embedded: Rclone
) -> None:
    # Closes design-review finding F4: the embedded failure contract now
    # matches the CLI's - RcloneCommandError regardless of execution mode -
    # not a bare RcCallError/OperationFailedError leaking through.
    missing = tmp_path / "does-not-exist.txt"

    with pytest.raises(RcloneCommandError):
        embedded.read_bytes(str(missing))


def test_purge_matches_cli_for_a_real_directory(
    tmp_path: Path, embedded: Rclone, cli: Rclone
) -> None:
    embedded_target = tmp_path / "embedded_target"
    cli_target = tmp_path / "cli_target"
    embedded_target.mkdir()
    cli_target.mkdir()
    (embedded_target / "sub").mkdir()
    (embedded_target / "sub" / "a.txt").write_bytes(b"x")
    (cli_target / "sub").mkdir()
    (cli_target / "sub" / "a.txt").write_bytes(b"x")

    embedded_result = embedded.purge(str(embedded_target))
    cli_result = cli.purge(str(cli_target))

    assert embedded_result.ok is True
    assert cli_result.ok is True
    assert not embedded_target.exists()
    assert not cli_target.exists()


def test_purge_never_raises_for_a_missing_directory(tmp_path: Path, embedded: Rclone) -> None:
    missing = tmp_path / "does-not-exist"

    result = embedded.purge(str(missing))

    assert result.ok is False


def test_cleanup_matches_cli_shape_for_a_local_path(
    tmp_path: Path, embedded: Rclone, cli: Rclone
) -> None:
    # The local backend doesn't support cleanup; both backends should
    # report failure the same way (never raising), not crash differently.
    embedded_result = embedded.cleanup(str(tmp_path))
    cli_result = cli.cleanup(str(tmp_path))

    assert embedded_result.ok is False
    assert cli_result.ok is False
