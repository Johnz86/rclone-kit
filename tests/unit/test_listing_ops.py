"""Unit tests for listing operations used by the public client."""

import json
import subprocess
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from helpers import ClientBackendAdapter
from rclone_kit.client import Rclone
from rclone_kit.command_flags import FLAG_CHECKERS, FLAG_FAST_LIST, FLAG_FILES_FROM
from rclone_kit.detail.listing_ops import (
    check_exists,
    check_is_synced,
    fetch_listremotes,
    fetch_ls,
    fetch_modtime,
    fetch_modtime_dt,
    fetch_size_file,
    fetch_size_files,
    fetch_stat,
    print_contents,
    stream_diff,
)
from rclone_kit.diff import DiffOption, DiffType
from rclone_kit.dir_listing import DirListing
from rclone_kit.file import File
from rclone_kit.process import Process
from rclone_kit.remote import Remote
from rclone_kit.types import SizeResult, SizeSuffix

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

_LSJSON_TWO_FILES = json.dumps(
    [
        {
            "Path": "a.txt",
            "Name": "a.txt",
            "Size": 1,
            "MimeType": "text/plain",
            "ModTime": "2024-01-01T00:00:00Z",
            "IsDir": False,
        },
        {
            "Path": "b.txt",
            "Name": "b.txt",
            "Size": 2,
            "MimeType": "text/plain",
            "ModTime": "2024-01-02T00:00:00Z",
            "IsDir": False,
        },
    ]
)


def _bare_rclone() -> Rclone:
    rclone = object.__new__(Rclone)
    rclone._backend = ClientBackendAdapter(rclone)
    return rclone


def test_fetch_ls_with_none_src_lists_remotes_as_root_dirs() -> None:
    rclone = _bare_rclone()
    remotes = [Remote(name="remoteA", rclone=rclone), Remote(name="remoteB", rclone=rclone)]
    rclone.listremotes = lambda: remotes

    result = fetch_ls(rclone._backend, rclone, None)

    assert [d.remote.name for d in result.dirs] == ["remoteA", "remoteB"]
    assert all(d.path.path == "" for d in result.dirs)


def test_fetch_ls_builds_expected_command_vector() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def run(cmd: list[str], check: bool = False, capture=None):
        del check, capture
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_LSJSON_ONE_FILE, stderr="")

    rclone._run = run

    result = fetch_ls(rclone._backend, rclone, "remote:bucket", max_depth=2)

    assert commands == [["lsjson", "--max-depth", "2", "remote:bucket"]]
    assert [f.path.name for f in result.files] == ["a.txt"]


def test_fetch_stat_raises_file_not_found_for_missing_path() -> None:
    rclone = _bare_rclone()
    rclone.ls = lambda *_args, **_kwargs: DirListing([])

    with pytest.raises(FileNotFoundError):
        fetch_stat(rclone, "remote:bucket/missing.txt")


def test_fetch_stat_returns_first_matching_file() -> None:
    rclone = _bare_rclone()

    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout=_LSJSON_ONE_FILE, stderr="")

    rclone._run = run

    result = fetch_stat(rclone, "remote:bucket/a.txt")

    assert result.path.name == "a.txt"


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


def test_fetch_listremotes_strips_trailing_colon() -> None:
    rclone = _bare_rclone()
    rclone._run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 0, stdout="remoteA:\nremoteB:\n", stderr=""
    )

    result = fetch_listremotes(rclone._backend, rclone)

    assert [r.name for r in result] == ["remoteA", "remoteB"]


def test_check_exists_true_when_listing_returns_entries() -> None:
    rclone = _bare_rclone()

    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout=_LSJSON_ONE_FILE, stderr="")

    rclone._run = run

    assert check_exists(rclone, "remote:bucket/a.txt") is True


def test_check_exists_false_on_called_process_error() -> None:
    rclone = _bare_rclone()

    def run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["rclone", "lsjson"])

    rclone._run = run

    assert check_exists(rclone, "remote:bucket/missing.txt") is False


def test_check_is_synced_true_when_check_succeeds() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def run(cmd: list[str], check: bool = False, capture=None):
        del capture
        commands.append(cmd)
        assert check is True
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    rclone._run = run

    assert check_is_synced(rclone._backend, "src:bucket", "dst:bucket") is True
    assert commands == [["check", "src:bucket", "dst:bucket"]]


def test_check_is_synced_false_on_called_process_error() -> None:
    rclone = _bare_rclone()

    def run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["rclone", "check"])

    rclone._run = run

    assert check_is_synced(rclone._backend, "src:bucket", "dst:bucket") is False


def test_fetch_size_file_raises_file_not_found_for_missing_path() -> None:
    rclone = _bare_rclone()
    rclone._run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 0, stdout="[]", stderr=""
    )

    with pytest.raises(FileNotFoundError):
        fetch_size_file(rclone, "remote:bucket/missing.txt")


def test_fetch_size_file_raises_value_error_for_multiple_matches() -> None:
    rclone = _bare_rclone()
    rclone._run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 0, stdout=_LSJSON_TWO_FILES, stderr=""
    )

    with pytest.raises(ValueError, match="More than one file found"):
        fetch_size_file(rclone, "remote:bucket/a.txt")


def test_fetch_size_file_returns_size_of_single_match() -> None:
    rclone = _bare_rclone()
    rclone._run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 0, stdout=_LSJSON_ONE_FILE, stderr=""
    )

    assert fetch_size_file(rclone, "remote:bucket/a.txt").as_int() == 1


def test_fetch_size_files_empty_input_returns_empty_result() -> None:
    rclone = _bare_rclone()

    result = fetch_size_files(rclone._backend, rclone, "remote:bucket", [])

    assert result == SizeResult(prefix="remote:bucket", total_size=0, file_sizes={})


def test_fetch_size_files_single_file_delegates_to_size_file() -> None:
    rclone = _bare_rclone()
    calls: list[str] = []

    def size_file(src: str) -> SizeSuffix:
        calls.append(src)
        return SizeSuffix(42)

    rclone.size_file = size_file

    result = fetch_size_files(rclone._backend, rclone, "remote:bucket", ["a.txt"])

    assert calls == ["remote:bucket/a.txt"]
    assert result == SizeResult(prefix="remote:bucket", total_size=42, file_sizes={"a.txt": 42})


def test_fetch_size_files_builds_expected_command_vector_and_aggregates() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def run(cmd: list[str], check: bool = False, capture=None):
        del check, capture
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_LSJSON_TWO_FILES, stderr="")

    rclone._run = run

    result = fetch_size_files(
        rclone._backend,
        rclone,
        "remote:bucket",
        ["a.txt", "b.txt"],
    )

    assert len(commands) == 1
    cmd = commands[0]
    assert cmd[:3] == ["lsjson", "remote:bucket", "--files-only"]
    assert "-R" in cmd
    assert FLAG_FILES_FROM in cmd
    assert result.total_size == 3
    assert result.file_sizes == {"a.txt": 1, "b.txt": 2}


def test_fetch_size_files_fast_list_warns() -> None:
    rclone = _bare_rclone()
    rclone._run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 0, stdout=_LSJSON_TWO_FILES, stderr=""
    )

    with pytest.warns(UserWarning, match="fast-list"):
        fetch_size_files(
            rclone._backend,
            rclone,
            "remote:bucket",
            ["a.txt", "b.txt"],
            fast_list=True,
        )


def test_print_contents_prints_read_text_result(capsys: pytest.CaptureFixture[str]) -> None:
    rclone = _bare_rclone()

    def read_text(src: str) -> str:
        del src
        return "file contents"

    rclone.read_text = read_text

    print_contents(rclone, "remote:bucket/a.txt")

    assert capsys.readouterr().out == "file contents\n"


def test_stream_diff_builds_expected_command_vector_and_streams_items() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def launch_process(
        cmd: list[str], capture: bool | None = None, log: Path | None = None
    ) -> Process:
        del capture, log
        commands.append(cmd)
        stdout = BytesIO(b"= same.txt\n- missing_src.txt\n")
        return cast(Process, SimpleNamespace(stdout=stdout))

    rclone._launch_process = launch_process

    items = list(stream_diff(rclone._backend, "src:bucket", "dst:bucket", checkers=5))

    assert commands == [
        [
            "check",
            "src:bucket",
            "dst:bucket",
            FLAG_CHECKERS,
            "5",
            "--log-level",
            "INFO",
            "--combined",
            "-",
            FLAG_FAST_LIST,
        ]
    ]
    assert [item.type for item in items] == [DiffType.EQUAL, DiffType.MISSING_ON_SRC]
    assert [item.path for item in items] == ["same.txt", "missing_src.txt"]


def test_stream_diff_missing_on_dst_adds_one_way_flag() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def launch_process(
        cmd: list[str], capture: bool | None = None, log: Path | None = None
    ) -> Process:
        del capture, log
        commands.append(cmd)
        return cast(Process, SimpleNamespace(stdout=BytesIO(b"")))

    rclone._launch_process = launch_process

    list(
        stream_diff(
            rclone._backend,
            "src:bucket",
            "dst:bucket",
            diff_option=DiffOption.MISSING_ON_DST,
        )
    )

    assert "--one-way" in commands[0]
    assert "--size-only" in commands[0]
