"""Unit tests for `rclone_kit.detail.listing_ops`, extracted from
`RcloneImpl` as part of the public-facade-split roadmap phase. `RcloneImpl`
methods delegate to these functions unchanged, so these tests exercise the
actual logic; `test_rclone_impl_contracts.py` covers that the delegation
itself still works.
"""

import json
import subprocess
from typing import cast

import pytest

from rclone_kit.detail.listing_ops import (
    check_exists,
    check_is_synced,
    fetch_listremotes,
    fetch_ls,
    fetch_modtime,
    fetch_modtime_dt,
    fetch_stat,
    print_contents,
)
from rclone_kit.dir_listing import DirListing
from rclone_kit.file import File
from rclone_kit.rclone_impl import RcloneImpl
from rclone_kit.remote import Remote

_LSJSON_ONE_FILE = json.dumps(
    [
        {
            "Path": "a.txt",
            "Name": "a.txt",
            "Size": 1,
            "MimeType": "text/plain",
            "ModTime": "2024-01-01T00:00:00Z",
            "IsDir": False,
        }
    ]
)


def _bare_rclone_impl() -> RcloneImpl:
    return object.__new__(RcloneImpl)


def test_fetch_ls_with_none_src_lists_remotes_as_root_dirs() -> None:
    rclone = _bare_rclone_impl()
    remotes = [Remote(name="remoteA", rclone=rclone), Remote(name="remoteB", rclone=rclone)]
    rclone.listremotes = lambda: remotes

    result = fetch_ls(rclone, None)

    assert [d.remote.name for d in result.dirs] == ["remoteA", "remoteB"]
    assert all(d.path.path == "" for d in result.dirs)


def test_fetch_ls_builds_expected_command_vector() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []

    def run(cmd: list[str], check: bool = False, capture=None):
        del check, capture
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_LSJSON_ONE_FILE, stderr="")

    rclone._run = run

    result = fetch_ls(rclone, "remote:bucket", max_depth=2)

    assert commands == [["lsjson", "--max-depth", "2", "remote:bucket"]]
    assert [f.path.name for f in result.files] == ["a.txt"]


def test_fetch_stat_raises_file_not_found_for_missing_path() -> None:
    rclone = _bare_rclone_impl()
    rclone.ls = lambda *_args, **_kwargs: DirListing([])

    with pytest.raises(FileNotFoundError):
        fetch_stat(rclone, "remote:bucket/missing.txt")


def test_fetch_stat_returns_first_matching_file() -> None:
    rclone = _bare_rclone_impl()

    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout=_LSJSON_ONE_FILE, stderr="")

    rclone._run = run

    result = fetch_stat(rclone, "remote:bucket/a.txt")

    assert result.path.name == "a.txt"


def test_fetch_modtime_delegates_to_stat() -> None:
    rclone = _bare_rclone_impl()

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

    rclone = _bare_rclone_impl()
    expected = datetime.fromisoformat("2024-01-01T00:00:00+00:00")

    class _FakeFile:
        def mod_time_dt(self):
            return expected

    def stat(src: str) -> File:
        del src
        return cast(File, _FakeFile())

    rclone.stat = stat

    assert fetch_modtime_dt(rclone, "remote:bucket/a.txt") == expected


def test_fetch_listremotes_strips_trailing_colon() -> None:
    rclone = _bare_rclone_impl()
    rclone._run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 0, stdout="remoteA:\nremoteB:\n", stderr=""
    )

    result = fetch_listremotes(rclone)

    assert [r.name for r in result] == ["remoteA", "remoteB"]


def test_check_exists_true_when_listing_returns_entries() -> None:
    rclone = _bare_rclone_impl()

    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout=_LSJSON_ONE_FILE, stderr="")

    rclone._run = run

    assert check_exists(rclone, "remote:bucket/a.txt") is True


def test_check_exists_false_on_called_process_error() -> None:
    rclone = _bare_rclone_impl()

    def run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["rclone", "lsjson"])

    rclone._run = run

    assert check_exists(rclone, "remote:bucket/missing.txt") is False


def test_check_is_synced_true_when_check_succeeds() -> None:
    rclone = _bare_rclone_impl()
    commands: list[list[str]] = []

    def run(cmd: list[str], check: bool = False, capture=None):
        del capture
        commands.append(cmd)
        assert check is True
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    rclone._run = run

    assert check_is_synced(rclone, "src:bucket", "dst:bucket") is True
    assert commands == [["check", "src:bucket", "dst:bucket"]]


def test_check_is_synced_false_on_called_process_error() -> None:
    rclone = _bare_rclone_impl()

    def run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["rclone", "check"])

    rclone._run = run

    assert check_is_synced(rclone, "src:bucket", "dst:bucket") is False


def test_print_contents_prints_read_text_result(capsys: pytest.CaptureFixture[str]) -> None:
    rclone = _bare_rclone_impl()

    def read_text(src: str) -> str:
        del src
        return "file contents"

    rclone.read_text = read_text

    print_contents(rclone, "remote:bucket/a.txt")

    assert capsys.readouterr().out == "file contents\n"
