"""Unit tests for `rclone_kit.env_file.load_env_file`."""

import os
from pathlib import Path

import pytest

from rclone_kit.env_file import load_env_file


def test_load_env_file_sets_plain_and_quoted_values_skips_comments_and_blanks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PLAIN_KEY", raising=False)
    monkeypatch.delenv("QUOTED_KEY", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "# a comment line",
                "",
                "PLAIN_KEY=plain_value",
                'QUOTED_KEY="quoted value"',
            ]
        ),
        encoding="utf-8",
    )

    load_env_file(env_path)

    assert os.environ["PLAIN_KEY"] == "plain_value"
    assert os.environ["QUOTED_KEY"] == "quoted value"


def test_load_env_file_does_not_override_existing_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALREADY_SET", "original_value")
    env_path = tmp_path / ".env"
    env_path.write_text("ALREADY_SET=overridden_value\n", encoding="utf-8")

    load_env_file(env_path)

    assert os.environ["ALREADY_SET"] == "original_value"


def test_load_env_file_does_nothing_when_file_missing(tmp_path: Path) -> None:
    load_env_file(tmp_path / "does-not-exist.env")
