"""Regression tests for the single public ``Rclone`` API."""

import inspect

import pytest

from rclone_kit import Rclone as PublicRclone
from rclone_kit.client import Rclone

PUBLIC_OPERATION_NAMES = {
    "check",
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
    "listremotes",
    "ls",
    "ls_stream",
    "modtime",
    "modtime_dt",
    "mount",
    "move",
    "move_to",
    "obscure",
    "purge",
    "read_bytes",
    "read_text",
    "save_to_db",
    "scan_missing_folders",
    "serve_http",
    "size_file",
    "size_files",
    "sync",
    "walk",
    "write_bytes",
    "write_text",
}


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
    )


def test_write_text_forwards_public_argument_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rclone = object.__new__(Rclone)
    calls: list[tuple[bytes, str]] = []
    monkeypatch.setattr(
        rclone,
        "write_bytes",
        lambda data, dst: calls.append((data, dst)),
    )

    rclone.write_text("content", "remote:bucket/file.txt")

    assert calls == [(b"content", "remote:bucket/file.txt")]
