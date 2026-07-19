"""Shared pytest fixtures for `tests/cloud/`.

Most files in this suite used to duplicate their own
`_generate_rclone_config()` plus a `unittest.TestCase.setUp()` calling
`skip_if_missing_cloud_env`. `do_spaces_config` replaces both with one
definition: requesting it builds the `Config` and skips the test via
`pytest.skip` when `DIGITAL_OCEAN_SPACES_ENV_VARS` are not set in the
environment, so individual test files no longer need to know which env
vars this provider requires or how its config section is shaped.

A handful of files intentionally do not use this fixture: `test_s3.py`
builds `S3Credentials`/`S3Client` directly rather than an rclone `Config`;
`test_copy_file_resumable_s3.py` and `test_read_write_text.py` build a
config `str` with an extra parameter (a port, or serve-http specific
settings) that does not fit this shape.
"""

import os

import pytest

from helpers import DIGITAL_OCEAN_SPACES_ENV_VARS, missing_cloud_env_vars
from rclone_kit import Config

_DIGITAL_OCEAN_SPACES_ENDPOINT = "sfo3.digitaloceanspaces.com"


def build_do_spaces_config() -> Config:
    """Build the `Config` for the `dst:` DigitalOcean Spaces remote used
    across this suite. Skips the test via `pytest.skip` when
    `DIGITAL_OCEAN_SPACES_ENV_VARS` are not set.

    A plain function rather than the `do_spaces_config` fixture body
    itself, so `tests/cloud/test_conftest.py` can call it directly without
    going through pytest's fixture-injection machinery.
    """
    missing = missing_cloud_env_vars(DIGITAL_OCEAN_SPACES_ENV_VARS)
    if missing:
        pytest.skip(f"Missing required environment variables: {', '.join(missing)}")
    bucket_key_secret = os.getenv("BUCKET_KEY_SECRET")
    bucket_key_public = os.getenv("BUCKET_KEY_PUBLIC")
    bucket_name = os.getenv("BUCKET_NAME")
    config_text = f"""
[dst]
type = s3
provider = DigitalOcean
access_key_id = {bucket_key_public}
secret_access_key = {bucket_key_secret}
endpoint = {_DIGITAL_OCEAN_SPACES_ENDPOINT}
bucket = {bucket_name}
"""
    return Config(config_text)


@pytest.fixture
def do_spaces_config() -> Config:
    return build_do_spaces_config()
