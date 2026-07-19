"""Unit tests for `rclone_kit.http_server._parse_files_and_dirs`.

Every fixture HTML string below mirrors the fixed shape rclone's own `serve
http` autoindex template produces: each entry is a `<tr class="file">` row
containing a `<span class="name"><a href="...">NAME</a></span>`, with a
trailing slash on the name distinguishing a directory from a file.
"""

from dataclasses import dataclass

import pytest

from rclone_kit.http_server import FileList, _parse_files_and_dirs

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
