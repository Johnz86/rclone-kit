"""Unit tests for `HttpServer.download_multi_threaded`'s `on_progress`
parameter.

`download()` and `size()` are monkeypatched rather than hitting a real
`serve http` instance - `tests/cloud/test_serve_http.py`/
`tests/live/s3/test_live_s3_http_and_fs.py` already cover the real HTTP
path; this file only needs to prove the progress-callback wiring itself.
"""

from pathlib import Path

import pytest

from rclone_kit.exceptions import HttpFetchError
from rclone_kit.http_server import HttpServer
from rclone_kit.types import Range


class _StubServerHandle:
    def dispose(self) -> None:
        pass


def _server() -> HttpServer:
    return HttpServer("http://localhost:8080", "", process=_StubServerHandle())


def _fake_download(_path: str, dst: Path, range: Range | None = None) -> Path:
    assert range is not None
    dst.write_bytes(b"x" * (range.end.as_int() - range.start.as_int()))
    return dst


def test_on_progress_reports_bytes_completed_per_chunk_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server()
    monkeypatch.setattr(server, "size", lambda _path: 30)
    monkeypatch.setattr(server, "download", _fake_download)
    calls: list[tuple[int, int]] = []

    server.download_multi_threaded(
        "file.bin",
        tmp_path / "out.bin",
        chunk_size=10,
        n_threads=2,
        on_progress=lambda completed, total: calls.append((completed, total)),
    )

    assert calls == [(10, 30), (20, 30), (30, 30)]


def test_on_progress_defaults_to_none_and_is_never_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server()
    monkeypatch.setattr(server, "size", lambda _path: 10)
    monkeypatch.setattr(server, "download", _fake_download)

    result = server.download_multi_threaded("file.bin", tmp_path / "out.bin", chunk_size=10)

    assert result == tmp_path / "out.bin"


def test_on_progress_is_not_called_for_a_failed_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server()
    monkeypatch.setattr(server, "size", lambda _path: 20)

    def _download_second_chunk_fails(_path: str, dst: Path, range: Range | None = None) -> Path:
        assert range is not None
        if range.start.as_int() == 10:
            raise HttpFetchError("file.bin", OSError("boom"))
        dst.write_bytes(b"x" * (range.end.as_int() - range.start.as_int()))
        return dst

    monkeypatch.setattr(server, "download", _download_second_chunk_fails)
    calls: list[tuple[int, int]] = []

    with pytest.raises(HttpFetchError):
        server.download_multi_threaded(
            "file.bin",
            tmp_path / "out.bin",
            chunk_size=10,
            n_threads=2,
            on_progress=lambda completed, total: calls.append((completed, total)),
        )

    assert calls == [(10, 20)]
