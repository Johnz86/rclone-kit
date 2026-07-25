"""Unit tests for listing operations used by the public client."""

from typing import cast

import pytest

from rclone_kit.client import Rclone
from rclone_kit.file import File
from rclone_kit.operations.listing_ops import (
    fetch_modtime,
    fetch_modtime_dt,
    print_contents,
)


def _bare_rclone() -> Rclone:
    return object.__new__(Rclone)


def test_fetch_modtime_delegates_to_stat() -> None:
    rclone = _bare_rclone()

    class _FakeFile:
        def mod_time(self) -> str:
            return "2024-01-01T00:00:00Z"

    def stat(src: str) -> File:
        del src
        return cast(File, _FakeFile())

    rclone.stat = stat

    assert fetch_modtime(rclone, "remote:bucket/a.txt") == "2024-01-01T00:00:00Z"


def test_fetch_modtime_dt_delegates_to_stat() -> None:
    from datetime import datetime

    rclone = _bare_rclone()
    expected = datetime.fromisoformat("2024-01-01T00:00:00+00:00")

    class _FakeFile:
        def mod_time_dt(self):
            return expected

    def stat(src: str) -> File:
        del src
        return cast(File, _FakeFile())

    rclone.stat = stat

    assert fetch_modtime_dt(rclone, "remote:bucket/a.txt") == expected


def test_print_contents_prints_read_text_result(capsys: pytest.CaptureFixture[str]) -> None:
    rclone = _bare_rclone()

    def read_text(src: str) -> str:
        del src
        return "file contents"

    rclone.read_text = read_text

    print_contents(rclone, "remote:bucket/a.txt")

    assert capsys.readouterr().out == "file contents\n"
