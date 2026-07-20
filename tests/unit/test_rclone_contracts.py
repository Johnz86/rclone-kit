import json
import subprocess
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from helpers import ClientBackendAdapter
from rclone_kit.client import Rclone
from rclone_kit.command_flags import (
    FLAG_CHECKERS,
    FLAG_FAST_LIST,
    FLAG_FILES_FROM,
    FLAG_LOW_LEVEL_RETRIES,
    FLAG_S3_NO_CHECK_BUCKET,
    FLAG_TRANSFERS,
)
from rclone_kit.diff import DiffOption, DiffType
from rclone_kit.dir_listing import DirListing
from rclone_kit.exceptions import RcloneCommandError
from rclone_kit.group_files import group_files
from rclone_kit.operations import mount_ops as mount_ops_module
from rclone_kit.process import Process
from rclone_kit.remote import Remote
from rclone_kit.types import ListingOption, Order, SizeResult


def _bare_rclone() -> Rclone:
    rclone = object.__new__(Rclone)
    rclone._backend = ClientBackendAdapter(rclone)
    return rclone


def _recording_run(commands: list[list[str]]):
    def run(
        cmd: list[str],
        check: bool = False,
        capture: bool | Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return run


def _lsjson_stdout(entries: list[dict]) -> str:
    return json.dumps(entries)


_LSJSON_TWO_FILES = _lsjson_stdout(
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


@dataclass(frozen=True)
class LsListingOptionCase:
    listing_option: ListingOption
    expected_flag: str | None


LS_LISTING_OPTION_ALL = LsListingOptionCase(ListingOption.ALL, None)
LS_LISTING_OPTION_DIRS_ONLY = LsListingOptionCase(ListingOption.DIRS_ONLY, "--dirs-only")
LS_LISTING_OPTION_FILES_ONLY = LsListingOptionCase(ListingOption.FILES_ONLY, "--files-only")

LS_LISTING_OPTION_CASES = [
    LS_LISTING_OPTION_ALL,
    LS_LISTING_OPTION_DIRS_ONLY,
    LS_LISTING_OPTION_FILES_ONLY,
]


def test_stat_raises_file_not_found_for_missing_path() -> None:
    rclone = _bare_rclone()
    rclone.ls = lambda *_args, **_kwargs: DirListing([])

    with pytest.raises(FileNotFoundError):
        rclone.stat("remote:bucket/missing.txt")


def test_read_bytes_raises_rclone_command_error_when_copy_fails() -> None:
    rclone = _bare_rclone()

    def copy_to(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["rclone", "copyto"], stderr="boom")

    rclone.copy_to = copy_to

    with pytest.raises(RcloneCommandError):
        rclone.read_bytes("remote:bucket/missing.txt")


def test_config_show_raises_rclone_command_error_on_failed_command() -> None:
    rclone = _bare_rclone()

    def run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["rclone", "config", "show"], stderr="boom")

    rclone._run = run

    with pytest.raises(RcloneCommandError):
        rclone.config_show()


def test_size_files_empty_input_returns_empty_result() -> None:
    rclone = _bare_rclone()

    result = rclone.size_files("remote:bucket", [])

    assert result == SizeResult(prefix="remote:bucket", total_size=0, file_sizes={})


def test_copy_files_empty_input_does_not_execute_rclone() -> None:
    rclone = _bare_rclone()
    rclone._run = lambda *_args, **_kwargs: pytest.fail("rclone must not run")

    assert rclone.copy_files("src:bucket", "dst:bucket", []) == []


def test_delete_files_empty_input_does_not_execute_rclone() -> None:
    rclone = _bare_rclone()
    rclone._run = lambda *_args, **_kwargs: pytest.fail("rclone must not run")

    result = rclone.delete_files([])

    assert result.ok
    assert result.completed[0].args == ["rclone", "delete", "--files-from", "[]"]


def test_copy_files_does_not_mutate_caller_arguments() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def run(
        cmd: list[str],
        check: bool = False,
        capture: bool | Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    rclone._run = run
    other_args = ["--metadata"]

    result = rclone.copy_files("src:bucket", "dst:bucket", ["folder/file"], other_args=other_args)

    assert result[0].ok
    assert other_args == ["--metadata"]
    assert commands[0][-2:] == ["--metadata", "--s3-no-check-bucket"]


def test_mount_respects_explicit_false_for_links(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mount_ops_module, "ensure_mount_supported", lambda: None)
    monkeypatch.setattr(mount_ops_module, "clean_mount", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mount_ops_module, "prepare_mount", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        mount_ops_module,
        "Mount",
        SimpleNamespace,
    )
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def launch(
        cmd: list[str],
        capture: bool | None = None,
        log: Path | None = None,
    ) -> Process:
        del capture, log
        commands.append(cmd)
        return object.__new__(Process)

    rclone._launch_process = launch

    rclone.mount(
        "remote:bucket",
        tmp_path / "mount",
        use_links=False,
        verbose=False,
    )

    assert "--links" not in commands[0]


def test_copy_to_builds_expected_command_vector() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    rclone.copy_to("src:bucket/a", "dst:bucket/a")

    assert commands == [
        ["copyto", "src:bucket/a", "dst:bucket/a", FLAG_S3_NO_CHECK_BUCKET, "--no-traverse"]
    ]


def test_copy_builds_expected_command_vector_with_defaults() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    rclone.copy("src:bucket", "dst:bucket")

    assert commands == [
        [
            "copy",
            "src:bucket",
            "dst:bucket",
            FLAG_CHECKERS,
            "1000",
            FLAG_TRANSFERS,
            "32",
            FLAG_LOW_LEVEL_RETRIES,
            "10",
            FLAG_S3_NO_CHECK_BUCKET,
        ]
    ]


def test_copy_files_builds_expected_command_vector() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    rclone.copy_files(
        "src:bucket",
        "dst:bucket",
        ["a.txt", "b.txt"],
        max_partition_workers=1,
    )

    assert len(commands) == 1
    cmd = commands[0]
    assert cmd[0] == "copy"
    assert cmd[1] == "src:bucket"
    assert cmd[2] == "dst:bucket"
    assert cmd[3] == "--files-from"
    assert cmd[5:] == [
        FLAG_CHECKERS,
        "1000",
        FLAG_TRANSFERS,
        "32",
        FLAG_LOW_LEVEL_RETRIES,
        "10",
        "--retries",
        "3",
        FLAG_S3_NO_CHECK_BUCKET,
    ]


def test_delete_files_builds_expected_command_vector() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)
    files = ["remote:bucket/a.txt", "remote:bucket/b.txt"]
    expected_groups = group_files(list(files))
    assert len(expected_groups) == 1
    expected_remote = next(iter(expected_groups))

    result = rclone.delete_files(files, max_partition_workers=1)

    assert result.ok
    assert len(commands) == 1
    cmd = commands[0]
    assert cmd[0] == "delete"
    assert cmd[1] == expected_remote
    assert cmd[2] == FLAG_FILES_FROM
    assert cmd[4:8] == [FLAG_CHECKERS, "1000", FLAG_TRANSFERS, "1000"]


def test_copy_bytes_builds_expected_command_vector(tmp_path: Path) -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []
    rclone._run = _recording_run(commands)

    rclone.copy_bytes("src:bucket/a", offset=10, length=20, outfile=tmp_path / "out.bin")

    assert commands == [["cat", "--offset", "10", "--count", "20", "src:bucket/a"]]


def test_ls_with_none_src_lists_remotes_as_root_dirs() -> None:
    rclone = _bare_rclone()
    remotes = [Remote(name="remoteA", rclone=rclone), Remote(name="remoteB", rclone=rclone)]
    rclone.listremotes = lambda: remotes

    result = rclone.ls(None)

    assert [d.remote.name for d in result.dirs] == ["remoteA", "remoteB"]
    assert all(d.path.path == "" for d in result.dirs)


def test_ls_builds_expected_command_vector_for_str_src() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def run(cmd: list[str], check: bool = False, capture=None):
        del check, capture
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_LSJSON_TWO_FILES, stderr="")

    rclone._run = run

    result = rclone.ls("remote:bucket", max_depth=2)

    assert commands == [["lsjson", "--max-depth", "2", "remote:bucket"]]
    assert [f.path.name for f in result.files] == ["a.txt", "b.txt"]


def test_ls_negative_max_depth_adds_recursive_flag() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def run(cmd: list[str], check: bool = False, capture=None):
        del check, capture
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_LSJSON_TWO_FILES, stderr="")

    rclone._run = run

    rclone.ls("remote:bucket", max_depth=-1)

    assert commands == [["lsjson", "--recursive", "remote:bucket"]]


@pytest.mark.parametrize("case", LS_LISTING_OPTION_CASES, ids=["all", "dirs_only", "files_only"])
def test_ls_listing_option_adds_expected_flag(case: LsListingOptionCase) -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def run(cmd: list[str], check: bool = False, capture=None):
        del check, capture
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_LSJSON_TWO_FILES, stderr="")

    rclone._run = run

    rclone.ls("remote:bucket", listing_option=case.listing_option)

    cmd = commands[0]
    if case.expected_flag is None:
        assert cmd == ["lsjson", "remote:bucket"]
    else:
        assert case.expected_flag in cmd


def test_ls_glob_filters_results_client_side() -> None:
    rclone = _bare_rclone()
    rclone._run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 0, stdout=_LSJSON_TWO_FILES, stderr=""
    )

    result = rclone.ls("remote:bucket", glob="bucket/a.*")

    assert [f.path.name for f in result.files] == ["a.txt"]


def test_ls_reverse_order_reverses_results() -> None:
    rclone = _bare_rclone()
    rclone._run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 0, stdout=_LSJSON_TWO_FILES, stderr=""
    )

    result = rclone.ls("remote:bucket", order=Order.REVERSE)

    assert [f.path.name for f in result.files] == ["b.txt", "a.txt"]


def test_ls_random_order_preserves_result_set() -> None:
    rclone = _bare_rclone()
    rclone._run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 0, stdout=_LSJSON_TWO_FILES, stderr=""
    )

    result = rclone.ls("remote:bucket", order=Order.RANDOM)

    assert {f.path.name for f in result.files} == {"a.txt", "b.txt"}


def test_size_files_single_file_delegates_to_size_file() -> None:
    rclone = _bare_rclone()
    calls: list[str] = []

    def size_file(src: str):
        calls.append(src)
        from rclone_kit.types import SizeSuffix

        return SizeSuffix(42)

    rclone.size_file = size_file

    result = rclone.size_files("remote:bucket", ["a.txt"])

    assert calls == ["remote:bucket/a.txt"]
    assert result == SizeResult(prefix="remote:bucket", total_size=42, file_sizes={"a.txt": 42})


def test_size_files_builds_expected_command_vector_and_aggregates(tmp_path: Path) -> None:
    del tmp_path
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def run(cmd: list[str], check: bool = False, capture=None):
        del check, capture
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_LSJSON_TWO_FILES, stderr="")

    rclone._run = run

    result = rclone.size_files("remote:bucket", ["a.txt", "b.txt"])

    assert len(commands) == 1
    cmd = commands[0]
    assert cmd[:3] == ["lsjson", "remote:bucket", "--files-only"]
    assert "-R" in cmd
    assert FLAG_FILES_FROM in cmd
    assert result.total_size == 3
    assert result.file_sizes == {"a.txt": 1, "b.txt": 2}


def test_size_files_fast_list_warns() -> None:
    rclone = _bare_rclone()
    rclone._run = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 0, stdout=_LSJSON_TWO_FILES, stderr=""
    )

    with pytest.warns(UserWarning, match="fast-list"):
        rclone.size_files("remote:bucket", ["a.txt", "b.txt"], fast_list=True)


def test_diff_builds_expected_command_vector_and_streams_items() -> None:
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

    items = list(rclone.diff("src:bucket", "dst:bucket", checkers=5))

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


def test_diff_missing_on_dst_adds_one_way_flag() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def launch_process(
        cmd: list[str], capture: bool | None = None, log: Path | None = None
    ) -> Process:
        del capture, log
        commands.append(cmd)
        return cast(Process, SimpleNamespace(stdout=BytesIO(b"")))

    rclone._launch_process = launch_process

    list(rclone.diff("src:bucket", "dst:bucket", diff_option=DiffOption.MISSING_ON_DST))

    assert "--one-way" in commands[0]
    assert "--size-only" in commands[0]


def test_copy_files_partitions_across_multiple_workers() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []
    file_contents: dict[str, str] = {}

    def run(cmd: list[str], check: bool = False, capture=None):
        del check, capture
        commands.append(cmd)
        files_from_index = cmd.index(FLAG_FILES_FROM)
        files_from_path = Path(cmd[files_from_index + 1])
        file_contents[cmd[1]] = files_from_path.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    rclone._run = run

    result = rclone.copy_files(
        "src:bucket",
        "dst:bucket",
        ["dirA/a.txt", "dirB/b.txt"],
        max_partition_workers=2,
    )

    assert len(result) == 2
    assert all(cp.ok for cp in result)
    assert len(commands) == 2
    src_paths = {cmd[1] for cmd in commands}
    assert src_paths == {"src:bucket/dirA", "src:bucket/dirB"}
    assert file_contents["src:bucket/dirA"] == "a.txt"
    assert file_contents["src:bucket/dirB"] == "b.txt"


def test_copy_files_partition_failure_raises_after_running_all_partitions() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def run(cmd: list[str], check: bool = False, capture=None):
        del check, capture
        commands.append(cmd)
        returncode = 1 if cmd[1] == "src:bucket/dirA" else 0
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="boom")

    rclone._run = run

    with pytest.raises(ValueError, match="boom"):
        rclone.copy_files(
            "src:bucket",
            "dst:bucket",
            ["dirA/a.txt", "dirB/b.txt"],
            max_partition_workers=2,
        )

    assert len(commands) == 2


def test_delete_files_partitions_across_multiple_workers() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []
    files = ["remoteA:bucketA/a.txt", "remoteB:bucketB/b.txt"]
    expected_groups = group_files(list(files))
    assert len(expected_groups) == 2
    rclone._run = _recording_run(commands)

    result = rclone.delete_files(files, max_partition_workers=2)

    assert result.ok
    assert len(commands) == 2
    remotes = {cmd[1] for cmd in commands}
    assert remotes == set(expected_groups.keys())


def test_delete_files_partition_failure_raises_after_running_all_partitions() -> None:
    rclone = _bare_rclone()
    commands: list[list[str]] = []
    files = ["remoteA:bucketA/a.txt", "remoteB:bucketB/b.txt"]

    def run(cmd: list[str], check: bool = False, capture=None):
        del check, capture
        commands.append(cmd)
        returncode = 1 if cmd[1] == "remoteA:bucketA" else 0
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="boom")

    rclone._run = run

    with pytest.raises(ValueError, match="boom"):
        rclone.delete_files(files, max_partition_workers=2)

    assert len(commands) == 2
