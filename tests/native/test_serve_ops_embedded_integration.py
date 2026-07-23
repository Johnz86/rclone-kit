"""Native-backed parity check for the embedded RC-backed serve operations
(ledger rows R03 `serve_webdav`, R04 `serve_http`, R05 resource tracking),
per the Wave H design (`native_c_abi_wave_h_review_and_design.md`).

Skipped automatically when no built native target exists (run
`scripts/native/build.py` first).
"""

from pathlib import Path

import pytest
from conftest import NATIVE_EXECUTABLE_AVAILABLE

from rclone_kit.client import Rclone
from rclone_kit.exceptions import UnsupportedEmbeddedOperationError
from rclone_kit.serve_handle import ServeHandle

pytestmark = pytest.mark.skipif(
    not NATIVE_EXECUTABLE_AVAILABLE,
    reason="No built native executable found; run scripts/native/build.py first.",
)


def test_serve_http_matches_cli_for_a_real_file(
    tmp_path: Path, embedded: Rclone, cli: Rclone
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "hello.txt").write_bytes(b"hello world")

    with embedded.serve_http(str(src)) as embedded_server:
        embedded_data = embedded_server.get("hello.txt")
    with cli.serve_http(str(src)) as cli_server:
        cli_data = cli_server.get("hello.txt")

    assert embedded_data == cli_data == b"hello world"


def test_serve_http_list_matches_directory_contents(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "sub").mkdir()
    (src / "a.txt").write_bytes(b"a")

    with embedded.serve_http(str(src)) as server:
        files, dirs = server.list("")

    assert files == ["a.txt"]
    assert dirs == ["sub/"]


def test_serve_http_shutdown_disposes_the_serve_handle(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    src.mkdir()

    server = embedded.serve_http(str(src))
    handle = server.process
    assert isinstance(handle, ServeHandle)
    assert handle.closed is False

    server.shutdown()

    assert handle.closed is True
    assert server.process is None


def test_serve_http_rejects_other_args(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    src.mkdir()

    with pytest.raises(UnsupportedEmbeddedOperationError):
        embedded.serve_http(str(src), other_args=["--foo"])


def test_serve_webdav_starts_and_stops(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    src.mkdir()

    handle = embedded.serve_webdav(str(src), "alice", "hunter2", addr="127.0.0.1:0")
    assert isinstance(handle, ServeHandle)
    assert isinstance(handle, ServeHandle)
    assert handle.id
    assert handle.addr

    handle.dispose()
    handle.dispose()  # idempotent

    assert handle.closed is True


def test_close_disposes_serve_handles_the_caller_never_disposed(
    tmp_path: Path, embedded: Rclone
) -> None:
    src = tmp_path / "src"
    src.mkdir()

    handle = embedded.serve_webdav(str(src), "alice", "hunter2", addr="127.0.0.1:0")
    assert isinstance(handle, ServeHandle)
    assert handle.closed is False

    embedded.close()

    assert handle.closed is True
