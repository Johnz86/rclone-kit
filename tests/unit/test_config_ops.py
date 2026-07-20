"""Unit tests for configuration operations used by the public client."""

import subprocess

import pytest

from helpers import ClientBackendAdapter
from rclone_kit.client import Rclone
from rclone_kit.config import Config
from rclone_kit.detail.config_ops import (
    check_is_s3,
    fetch_s3_credentials,
    obscure_password,
)
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


def _config() -> Config:
    return Config(_CONFIG_TEXT)


def test_check_is_s3_true_for_s3_and_b2_remotes() -> None:
    config = _config()

    assert check_is_s3(config, "do-remote:bucket/key.txt") is True
    assert check_is_s3(config, "b2-remote:bucket/key.txt") is True


def test_check_is_s3_false_for_non_s3_remote() -> None:
    config = _config()

    assert check_is_s3(config, "local-remote:bucket/key.txt") is False


def test_check_is_s3_false_for_unknown_remote() -> None:
    config = _config()

    assert check_is_s3(config, "missing-remote:bucket/key.txt") is False


def test_check_is_s3_false_for_malformed_path() -> None:
    config = _config()

    assert check_is_s3(config, "not-a-valid-s3-path") is False


def test_fetch_s3_credentials_raises_for_unknown_remote() -> None:
    config = _config()

    with pytest.raises(ValueError, match="not found in rclone config"):
        fetch_s3_credentials(config, "missing-remote:bucket/key.txt")


def test_fetch_s3_credentials_raises_for_non_s3_remote() -> None:
    config = _config()

    with pytest.raises(ValueError, match="is not an S3 remote"):
        fetch_s3_credentials(config, "local-remote:bucket/key.txt")


def test_fetch_s3_credentials_uses_explicit_provider() -> None:
    config = _config()

    creds = fetch_s3_credentials(config, "do-remote:bucket/key.txt")

    assert creds.bucket_name == "bucket"
    assert creds.provider == S3Provider.DIGITAL_OCEAN
    assert creds.access_key_id == "AKIAEXAMPLE"
    assert creds.secret_access_key == "super-secret"  # noqa: S105
    assert creds.endpoint_url == "s3.amazonaws.com"


def test_fetch_s3_credentials_defaults_provider_for_b2() -> None:
    config = _config()

    creds = fetch_s3_credentials(config, "b2-remote:bucket/key.txt")

    assert creds.provider == S3Provider.BACKBLAZE
    assert creds.access_key_id == "accountid"
    assert creds.secret_access_key == "appkey"  # noqa: S105


def test_obscure_password_builds_expected_command_vector() -> None:
    rclone = object.__new__(Rclone)
    backend = ClientBackendAdapter(rclone)
    commands: list[list[str]] = []

    def run(cmd: list[str], check: bool = False, capture=None) -> subprocess.CompletedProcess[str]:
        del check, capture
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="  obscured-value  \n", stderr="")

    rclone._run = run

    result = obscure_password(backend, "hunter2")

    assert commands == [["obscure", "hunter2"]]
    assert result == "obscured-value"
