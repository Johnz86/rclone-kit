"""Unit tests for `rclone_kit.http_server._parse_files_and_dirs`.

Every fixture HTML string below mirrors the fixed shape rclone's own `serve
http` autoindex template produces: each entry is a `<tr class="file">` row
containing a `<span class="name"><a href="...">NAME</a></span>`, with a
trailing slash on the name distinguishing a directory from a file.
"""

from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from rclone_kit import http_server as http_server_module
from rclone_kit.http_server import FileList, HttpServer, _parse_files_and_dirs
from rclone_kit.process import Process
from rclone_kit.types import Range

_TABLE_HEADER = """
<html>
<body>
<table>
"""

_TABLE_FOOTER = """
</table>
</body>
</html>
"""


def _row(name: str) -> str:
    return f'<tr class="file"><td><span class="name"><a href="{name}">{name}</a></span></td></tr>'


def _stub_process() -> Process:
    return object.__new__(Process)


@dataclass(frozen=True)
class ParseCase:
    html: str
    expected: FileList


MIXED_FILES_AND_DIRS_CASE = ParseCase(
    html=_TABLE_HEADER
    + _row("report.txt")
    + _row("photos/")
    + _row("archive.tar.gz")
    + _row("backups/")
    + _TABLE_FOOTER,
    expected=FileList(
        dirs=["photos/", "backups/"],
        files=["report.txt", "archive.tar.gz"],
    ),
)

EMPTY_LISTING_CASE = ParseCase(
    html=_TABLE_HEADER + _TABLE_FOOTER,
    expected=FileList(dirs=[], files=[]),
)

DIRECTORIES_ONLY_CASE = ParseCase(
    html=_TABLE_HEADER + _row("photos/") + _row("backups/") + _TABLE_FOOTER,
    expected=FileList(dirs=["photos/", "backups/"], files=[]),
)

FILES_ONLY_CASE = ParseCase(
    html=_TABLE_HEADER + _row("report.txt") + _row("archive.tar.gz") + _TABLE_FOOTER,
    expected=FileList(dirs=[], files=["report.txt", "archive.tar.gz"]),
)

HTML_ENTITY_NAME_CASE = ParseCase(
    html=_TABLE_HEADER
    + '<tr class="file"><td><span class="name"><a href="A%20%26%20B.txt">A &amp; B.txt</a></span></td></tr>'
    + _TABLE_FOOTER,
    expected=FileList(dirs=[], files=["A & B.txt"]),
)

PARSE_CASES = [
    MIXED_FILES_AND_DIRS_CASE,
    EMPTY_LISTING_CASE,
    DIRECTORIES_ONLY_CASE,
    FILES_ONLY_CASE,
    HTML_ENTITY_NAME_CASE,
]
PARSE_IDS = [
    "mixed_files_and_dirs",
    "empty_listing",
    "directories_only",
    "files_only",
    "html_entity_name",
]


@pytest.mark.parametrize("case", PARSE_CASES, ids=PARSE_IDS)
def test_parse_files_and_dirs(case: ParseCase) -> None:
    result = _parse_files_and_dirs(case.html)
    assert result == case.expected


def test_file_url_escapes_remote_path_without_platform_conversion() -> None:
    server = HttpServer("http://localhost:8080/", "", process=_stub_process())

    assert (
        server._get_file_url("folder/a file #1.txt")
        == "http://localhost:8080/folder/a%20file%20%231.txt"
    )


def test_get_returns_download_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    server = HttpServer("http://localhost:8080", "", process=_stub_process())
    failure = OSError("download failed")
    monkeypatch.setattr(server, "download", lambda *_args, **_kwargs: failure)

    assert server.get("missing.txt") is failure


class _ShortRangeResponse:
    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, chunk_size: int):
        del chunk_size
        yield b"ab"


def test_download_rejects_short_ranged_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = HttpServer("http://localhost:8080", "", process=_stub_process())
    destination = tmp_path / "download"
    monkeypatch.setattr(
        http_server_module.httpx,
        "stream",
        lambda *_args, **_kwargs: _ShortRangeResponse(),
    )
    monkeypatch.setattr(http_server_module, "_range", lambda _count: iter((0,)))
    monkeypatch.setattr(http_server_module.time, "sleep", lambda _seconds: None)

    with pytest.warns(UserWarning):
        result = server.download("file.bin", destination, Range(0, 4))

    assert isinstance(result, Exception)
    assert not destination.exists()


def test_download_after_shutdown_returns_failure(tmp_path: Path) -> None:
    server = HttpServer("http://localhost:8080", "", process=_stub_process())
    server.process = None

    result = server.download("file.bin", tmp_path / "download")

    assert isinstance(result, RuntimeError)
