"""Native-backed parity check for the embedded RC-backed listing/stat
operations (ledger rows M02, L05, L06/L07, L08, L10), against the CLI
backend built from the exact same commit.

Uses local filesystem paths, so no configured remote is required; reuses
the shared, already-initialized `native_runtime` session fixture (see
`conftest.py`) rather than initializing its own.

Skipped automatically when no built native target exists (run
`scripts/native/build.py` first).
"""

from pathlib import Path

import pytest

from conftest import EXECUTABLE_PATH, NATIVE_EXECUTABLE_AVAILABLE
from rclone_kit.client import Rclone
from rclone_kit.native.runtime import RcloneRuntime

pytestmark = pytest.mark.skipif(
    not NATIVE_EXECUTABLE_AVAILABLE,
    reason="No built native executable found; run scripts/native/build.py first.",
)


@pytest.fixture
def embedded(native_runtime: RcloneRuntime) -> Rclone:
    return Rclone(None, execution="embedded", runtime=native_runtime)


@pytest.fixture
def cli() -> Rclone:
    assert EXECUTABLE_PATH is not None
    return Rclone(None, rclone_exe=EXECUTABLE_PATH)


def test_stat_matches_cli_for_an_existing_file(
    tmp_path: Path, embedded: Rclone, cli: Rclone
) -> None:
    (tmp_path / "hello.txt").write_bytes(b"hello world")
    src = f"{tmp_path}/hello.txt"

    embedded_file = embedded.stat(src)
    cli_file = cli.stat(src)

    assert embedded_file.size == cli_file.size == len(b"hello world")
    assert embedded_file.name == cli_file.name == "hello.txt"
    assert not embedded_file.path.is_dir
    assert not cli_file.path.is_dir


def test_stat_raises_file_not_found_matching_cli(
    tmp_path: Path, embedded: Rclone, cli: Rclone
) -> None:
    src = f"{tmp_path}/does-not-exist.txt"

    with pytest.raises(FileNotFoundError):
        embedded.stat(src)
    with pytest.raises(FileNotFoundError):
        cli.stat(src)


def test_modtime_is_transitively_embedded_through_stat(
    tmp_path: Path, embedded: Rclone
) -> None:
    (tmp_path / "hello.txt").write_bytes(b"hello")
    src = f"{tmp_path}/hello.txt"

    # modtime()/modtime_dt() have no execution branch of their own; they
    # only work here because stat() dispatches to the embedded adapter.
    assert embedded.modtime(src) == embedded.stat(src).mod_time()
    assert embedded.modtime_dt(src) == embedded.stat(src).mod_time_dt()


def test_size_file_matches_cli(tmp_path: Path, embedded: Rclone, cli: Rclone) -> None:
    (tmp_path / "hello.txt").write_bytes(b"hello world!")
    src = f"{tmp_path}/hello.txt"

    assert embedded.size_file(src).as_int() == cli.size_file(src).as_int() == len(
        b"hello world!"
    )


def test_exists_matches_cli_for_present_and_missing_paths(
    tmp_path: Path, embedded: Rclone, cli: Rclone
) -> None:
    (tmp_path / "hello.txt").write_bytes(b"hello")
    present = f"{tmp_path}/hello.txt"
    missing = f"{tmp_path}/nope.txt"

    assert embedded.exists(present) is True
    assert cli.exists(present) is True
    assert embedded.exists(missing) is False
    assert cli.exists(missing) is False


def test_listremotes_returns_a_list_of_remote_when_none_configured(
    embedded: Rclone,
) -> None:
    assert embedded.listremotes() == []


def test_config_paths_returns_a_config_cache_temp_triple(embedded: Rclone) -> None:
    paths = embedded.config_paths()

    assert len(paths) == 3
    assert all(isinstance(p, Path) for p in paths)
