"""Tests for the structural rclone command-execution boundary."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from rclone_kit.backend import CliRcloneBackend
from rclone_kit.command_flags import FLAG_S3_NO_CHECK_BUCKET
from rclone_kit.config import Config
from rclone_kit.detail.transfer_ops import copy_file_to
from rclone_kit.process import Process


@dataclass
class RecordingBackend:
    commands: list[tuple[str, ...]] = field(default_factory=list)
    options: list[tuple[bool, bool | Path | None]] = field(default_factory=list)

    def run(
        self,
        command: tuple[str, ...],
        *,
        check: bool = False,
        capture: bool | Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        self.options.append((check, capture))
        return subprocess.CompletedProcess(list(command), 0, stdout="", stderr="")

    def launch(
        self,
        command: tuple[str, ...],
        *,
        capture: bool | None = None,
        log: Path | None = None,
    ) -> Process:
        del command, capture, log
        return cast(Process, object())


def test_recording_backend_exercises_operation_without_subclassing() -> None:
    backend = RecordingBackend()

    copy_file_to(backend, "src:bucket/a", "dst:bucket/a", check=False)

    assert backend.commands == [
        (
            "copyto",
            "src:bucket/a",
            "dst:bucket/a",
            FLAG_S3_NO_CHECK_BUCKET,
            "--no-traverse",
        )
    ]
    assert backend.options == [(False, None)]


def test_cli_backend_forwards_immutable_command_and_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = Config("[remote]\ntype = local\n")
    executable = tmp_path / "rclone"
    backend = CliRcloneBackend(config, executable)
    calls: list[tuple[list[str], Config, Path, bool, bool | Path | None]] = []

    def execute(
        command: list[str],
        rclone_config: Config,
        rclone_exe: Path,
        *,
        check: bool,
        capture: bool | Path | None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, rclone_config, rclone_exe, check, capture))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("rclone_kit.backend.rclone_execute", execute)

    backend.run(("listremotes",), check=True, capture=True)

    assert calls == [(["listremotes"], config, executable, True, True)]
