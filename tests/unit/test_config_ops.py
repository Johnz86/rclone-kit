"""Unit tests for `rclone_kit.detail.config_ops`, extracted from
`RcloneImpl` as part of the public-facade-split roadmap phase. `RcloneImpl`
methods delegate to these functions unchanged, so these tests exercise the
actual logic; `test_rclone_impl_contracts.py` and `test_config_discovery.py`
cover that the delegation itself still works.
"""

import subprocess

import pytest

from rclone_kit.config import Config
from rclone_kit.detail.config_ops import (
    check_is_s3,
    fetch_s3_credentials,
    obscure_password,
)
from rclone_kit.rclone_impl import RcloneImpl
from rclone_kit.s3.types import S3Provider

_CONFIG_TEXT = """
[do-remote]
type = s3
provider = DigitalOcean
access_key_id = AKIAEXAMPLE
secret_access_key = super-secret
endpoint = s3.amazonaws.com

[b2-remote]
type = b2
account = accountid
key = appkey

[local-remote]
type = local
"""


def _rclone_with_config() -> RcloneImpl:
    rclone = object.__new__(RcloneImpl)
    rclone.config = Config(_CONFIG_TEXT)
    return rclone


def test_check_is_s3_true_for_s3_and_b2_remotes() -> None:
    rclone = _rclone_with_config()

    assert check_is_s3(rclone, "do-remote:bucket/key.txt") is True
    assert check_is_s3(rclone, "b2-remote:bucket/key.txt") is True


def test_check_is_s3_false_for_non_s3_remote() -> None:
    rclone = _rclone_with_config()

    assert check_is_s3(rclone, "local-remote:bucket/key.txt") is False


def test_check_is_s3_false_for_unknown_remote() -> None:
    rclone = _rclone_with_config()

    assert check_is_s3(rclone, "missing-remote:bucket/key.txt") is False


def test_check_is_s3_false_for_malformed_path() -> None:
    rclone = _rclone_with_config()

    assert check_is_s3(rclone, "not-a-valid-s3-path") is False


def test_fetch_s3_credentials_raises_for_unknown_remote() -> None:
    rclone = _rclone_with_config()

    with pytest.raises(ValueError, match="not found in rclone config"):
        fetch_s3_credentials(rclone, "missing-remote:bucket/key.txt")


def test_fetch_s3_credentials_raises_for_non_s3_remote() -> None:
    rclone = _rclone_with_config()

    with pytest.raises(ValueError, match="is not an S3 remote"):
        fetch_s3_credentials(rclone, "local-remote:bucket/key.txt")


def test_fetch_s3_credentials_uses_explicit_provider() -> None:
    rclone = _rclone_with_config()

    creds = fetch_s3_credentials(rclone, "do-remote:bucket/key.txt")

    assert creds.bucket_name == "bucket"
    assert creds.provider == S3Provider.DIGITAL_OCEAN
    assert creds.access_key_id == "AKIAEXAMPLE"
    assert creds.secret_access_key == "super-secret"  # noqa: S105
    assert creds.endpoint_url == "s3.amazonaws.com"


def test_fetch_s3_credentials_defaults_provider_for_b2() -> None:
    rclone = _rclone_with_config()

    creds = fetch_s3_credentials(rclone, "b2-remote:bucket/key.txt")

    assert creds.provider == S3Provider.BACKBLAZE
    assert creds.access_key_id == "accountid"
    assert creds.secret_access_key == "appkey"  # noqa: S105


def test_obscure_password_builds_expected_command_vector() -> None:
    rclone = object.__new__(RcloneImpl)
    commands: list[list[str]] = []

    def run(cmd: list[str], check: bool = False, capture=None) -> subprocess.CompletedProcess[str]:
        del check, capture
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="  obscured-value  \n", stderr="")

    rclone._run = run

    result = obscure_password(rclone, "hunter2")

    assert commands == [["obscure", "hunter2"]]
    assert result == "obscured-value"
