"""Regression tests for the single public ``Rclone`` API."""

import inspect
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from rclone_kit import Rclone as PublicRclone
from rclone_kit.client import Rclone
from rclone_kit.command_flags import FLAG_S3_NO_CHECK_BUCKET
from rclone_kit.config import Config
from rclone_kit.http_server import HttpServer
from rclone_kit.process import Process

PUBLIC_OPERATION_NAMES = {
    "cleanup",
    "copy",
    "copy_bytes",
    "copy_dir",
    "copy_file_s3",
    "copy_file_s3_resumable",
    "copy_files",
    "copy_remote",
    "copy_to",
    "cwd",
    "delete_files",
    "diff",
    "exists",
    "filesystem",
    "is_s3",
    "is_synced",
    "launch_server",
    "listremotes",
    "ls",
    "ls_stream",
    "modtime",
    "modtime_dt",
    "mount",
    "obscure",
    "purge",
    "read_bytes",
    "read_text",
    "remote_control",
    "save_to_db",
    "scan_missing_folders",
    "serve_http",
    "size_file",
    "size_files",
    "walk",
    "webgui",
    "write_bytes",
    "write_text",
}


@dataclass
class RecordingBackend:
    commands: list[tuple[str, ...]] = field(default_factory=list)

    def run(
        self,
        command: tuple[str, ...],
        *,
        check: bool = False,
        capture: bool | Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture
        self.commands.append(command)
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


def test_package_root_reexports_concrete_client() -> None:
    assert PublicRclone is Rclone


def test_public_operations_remain_available() -> None:
    assert set(vars(Rclone)) >= PUBLIC_OPERATION_NAMES


def test_write_text_uses_public_parameter_order() -> None:
    assert tuple(inspect.signature(Rclone.write_text).parameters) == ("self", "text", "dst")


def test_write_bytes_keeps_curated_public_contract() -> None:
    assert tuple(inspect.signature(Rclone.write_bytes).parameters) == ("self", "data", "dst")


def test_serve_http_keeps_curated_public_contract() -> None:
    assert tuple(inspect.signature(Rclone.serve_http).parameters) == (
        "self",
        "src",
        "addr",
        "other_args",
    )


def test_custom_backend_does_not_require_cli_executable_resolution() -> None:
    backend = RecordingBackend()

    rclone = Rclone(Config(None), backend=backend)

    assert rclone._backend is backend


def test_write_text_forwards_public_argument_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rclone = Rclone(Config(None), backend=RecordingBackend())
    calls: list[tuple[bytes, str]] = []
    monkeypatch.setattr(
        rclone,
        "write_bytes",
        lambda data, dst: calls.append((data, dst)),
    )

    rclone.write_text("content", "remote:bucket/file.txt")

    assert calls == [(b"content", "remote:bucket/file.txt")]


def test_write_bytes_builds_copy_command_for_non_s3_destination() -> None:
    backend = RecordingBackend()
    rclone = Rclone(Config(None), backend=backend)

    rclone.write_bytes(b"content", "remote:bucket/file.bin")

    command = backend.commands[0]
    assert command[0] == "copyto"
    assert command[2:] == (
        "remote:bucket/file.bin",
        FLAG_S3_NO_CHECK_BUCKET,
        "--no-traverse",
    )


def test_serve_http_uses_curated_cache_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rclone = Rclone(Config(None), backend=RecordingBackend())
    calls: list[tuple[Any, ...]] = []
    expected = cast(HttpServer, object())

    def launch(*args: Any, **kwargs: Any) -> HttpServer:
        calls.append((*args, kwargs))
        return expected

    monkeypatch.setattr("rclone_kit.client.launch_http_server", launch)

    result = rclone.serve_http(
        "remote:bucket",
        addr="localhost:8080",
        other_args=["--read-only"],
    )

    assert result is expected
    assert calls == [
        (
            rclone._backend,
            "remote:bucket",
            "minimal",
            {
                "addr": "localhost:8080",
                "other_args": ["--read-only"],
            },
        )
    ]
