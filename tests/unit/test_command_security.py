import os
import stat
import subprocess

import pytest

from rclone_kit.completed_process import CompletedProcess
from rclone_kit.util import clear_temp_config_file, format_command, make_temp_config_file

FIRST_CREDENTIAL = "credential-one"
SECOND_CREDENTIAL = "credential-two"
THIRD_CREDENTIAL = "credential-three"


def test_format_command_redacts_separate_and_inline_credentials() -> None:
    formatted = format_command(
        [
            "rclone",
            "rcd",
            "--rc-pass",
            FIRST_CREDENTIAL,
            f"--token={SECOND_CREDENTIAL}",
            "--s3-access-key-id",
            THIRD_CREDENTIAL,
            "remote:bucket",
        ]
    )

    assert FIRST_CREDENTIAL not in formatted
    assert SECOND_CREDENTIAL not in formatted
    assert THIRD_CREDENTIAL not in formatted
    assert formatted.count("<redacted>") == 3
    assert "remote:bucket" in formatted


def test_completed_process_string_redacts_credentials() -> None:
    process = subprocess.CompletedProcess(
        ["rclone", "rcd", "--rc-pass", FIRST_CREDENTIAL],
        returncode=1,
    )

    rendered = str(CompletedProcess.from_subprocess(process))

    assert FIRST_CREDENTIAL not in rendered
    assert "<redacted>" in rendered


def test_temporary_config_cleanup_removes_private_directory() -> None:
    config_path = make_temp_config_file()
    config_directory = config_path.parent

    clear_temp_config_file(config_path)

    assert not config_directory.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
def test_temporary_config_has_owner_only_permissions() -> None:
    config_path = make_temp_config_file()
    try:
        mode = stat.S_IMODE(config_path.stat().st_mode)
        assert mode == stat.S_IRUSR | stat.S_IWUSR
    finally:
        clear_temp_config_file(config_path)
