"""Unit tests for serving operations used by the public client."""

from pathlib import Path
from typing import cast

import pytest

from helpers import ClientBackendAdapter
from rclone_kit.client import Rclone
from rclone_kit.detail.serve_ops import launch_http_server, launch_webdav_server
from rclone_kit.process import Process


class _FakeProcess:
    def __init__(self, poll_result: int | None) -> None:
        self._poll_result = poll_result

    def poll(self) -> int | None:
        return self._poll_result


def _bare_rclone() -> Rclone:
    rclone = object.__new__(Rclone)
    rclone._backend = ClientBackendAdapter(rclone)
    return rclone


def test_launch_http_server_builds_expected_command_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rclone_kit.detail.serve_ops.time.sleep", lambda _s: None)
    rclone = _bare_rclone()
    commands: list[tuple[list[str], Path | None]] = []

    def launch(cmd: list[str], capture: bool | None = None, log: Path | None = None) -> Process:
        del capture
        commands.append((cmd, log))
        return cast(Process, _FakeProcess(None))

    rclone._launch_process = launch

    server = launch_http_server(
        rclone._backend,
        "remote:bucket/path",
        cache_mode="minimal",
        addr="localhost:1234",
    )

    assert commands[0][0] == [
        "serve",
        "http",
        "--addr",
        "localhost:1234",
        "remote:bucket/path",
        "--vfs-disk-space-total-size",
        "0",
        "--vfs-read-chunk-size-limit",
        "512M",
        "--vfs-cache-mode",
        "minimal",
    ]
    assert server.url == "http://localhost:1234"
    assert server.subpath == "bucket/path"


def test_launch_http_server_includes_log_flags_when_serve_http_log_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("rclone_kit.detail.serve_ops.time.sleep", lambda _s: None)
    rclone = _bare_rclone()
    commands: list[list[str]] = []
    log_path = tmp_path / "serve.log"

    def launch(cmd: list[str], capture: bool | None = None, log: Path | None = None) -> Process:
        del capture, log
        commands.append(cmd)
        return cast(Process, _FakeProcess(None))

    rclone._launch_process = launch

    launch_http_server(
        rclone._backend,
        "remote:bucket",
        cache_mode=None,
        addr="localhost:1234",
        serve_http_log=log_path,
    )

    assert commands[0][-3:] == ["--log-file", str(log_path), "-vvvv"]


def test_launch_http_server_raises_when_process_fails_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rclone_kit.detail.serve_ops.time.sleep", lambda _s: None)
    rclone = _bare_rclone()
    rclone._launch_process = lambda *_args, **_kwargs: cast(Process, _FakeProcess(1))

    with pytest.raises(ValueError, match="HTTP serve process failed to start"):
        launch_http_server(
            rclone._backend,
            "remote:bucket",
            cache_mode=None,
            addr="localhost:1234",
        )


def test_launch_webdav_server_builds_expected_command_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rclone_kit.detail.serve_ops.time.sleep", lambda _s: None)
    rclone = _bare_rclone()
    commands: list[list[str]] = []

    def launch(cmd: list[str], capture: bool | None = None, log: Path | None = None) -> Process:
        del capture, log
        commands.append(cmd)
        return cast(Process, _FakeProcess(None))

    rclone._launch_process = launch

    launch_webdav_server(
        rclone._backend,
        "remote:bucket",
        user="alice",
        password="hunter2",  # noqa: S106
    )

    assert commands[0] == [
        "serve",
        "webdav",
        "--addr",
        "localhost:2049",
        "remote:bucket",
        "--user",
        "alice",
        "--pass",
        "hunter2",
    ]


def test_launch_webdav_server_raises_when_process_fails_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rclone_kit.detail.serve_ops.time.sleep", lambda _s: None)
    rclone = _bare_rclone()
    rclone._launch_process = lambda *_args, **_kwargs: cast(Process, _FakeProcess(1))

    with pytest.raises(ValueError, match="NFS serve process failed to start"):
        launch_webdav_server(
            rclone._backend,
            "remote:bucket",
            user="alice",
            password="hunter2",  # noqa: S106
        )
