"""Live test file.

Exercises the scoped HTTP download server (`serve_http`) and the
`pathlib`-like remote filesystem interface (`filesystem`/`RemoteFS`) - the
other half of the "the general API is backend-agnostic" claim, alongside
the S3-boundary tests in `test_live_gdrive_config.py`.
"""

from pathlib import Path

import pytest

from rclone_kit import Range, Rclone

pytestmark = pytest.mark.live_gdrive


def test_serve_http_reports_size_and_serves_full_and_ranged_content(
    live_rclone: Rclone, live_test_prefix: str
) -> None:
    live_rclone.write_bytes(b"0123456789", f"{live_test_prefix}/served.txt")

    with live_rclone.serve_http(live_test_prefix) as server:
        assert server.size("served.txt") == 10
        assert server.get("served.txt") == b"0123456789"
        assert server.get("served.txt", range=Range(start=2, end=5)) == b"234"


def test_remote_fs_reads_and_writes_through_fspath(
    live_rclone: Rclone, live_test_prefix: str
) -> None:
    with live_rclone.filesystem(live_test_prefix) as remote_fs:
        root = remote_fs.cwd()
        manifest = root / "manifest.json"

        manifest.write_text('{"status": "ready"}')

        assert manifest.exists()
        assert manifest.read_text() == '{"status": "ready"}'


def test_remote_fs_walk_begin_finds_nested_files(
    live_rclone: Rclone, live_test_prefix: str, local_source_tree: Path
) -> None:
    live_rclone.copy(str(local_source_tree), live_test_prefix, check=True)

    found_files: set[str] = set()
    with live_rclone.filesystem(live_test_prefix) as remote_fs:
        root = remote_fs.cwd()
        with root.walk_begin() as walker:
            for _current, _dirnames, filenames in walker:
                found_files.update(filenames)

    assert found_files == {"a.txt", "b.txt", "c.txt"}
