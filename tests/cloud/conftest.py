"""Shared pytest fixtures for `tests/cloud/`.

Most files in this suite used to duplicate their own
`_generate_rclone_config()` plus a `unittest.TestCase.setUp()` calling
`skip_if_missing_cloud_env`. `do_spaces_config`/`build_do_spaces_config`
replace both with one definition: requesting it builds the `Config` and
skips the test via `pytest.skip` when `DIGITAL_OCEAN_SPACES_ENV_VARS` are
not set in the environment, so individual test files no longer need to know
which env vars this provider requires or how its config section is shaped.
`test_rclone_config.py` uses `do_spaces_config` directly (it only parses the
config text, never constructs an `Rclone`); every other file constructs a
client through `cloud_rclone` below instead.

`cloud_rclone` shares one process-wide `RcloneRuntime` (`cloud_runtime`,
session-scoped) across the whole suite: the native ABI permits initializing
a given runtime exactly once per process (see `tests/native/conftest.py`'s
`native_runtime` fixture for the same constraint), so a fresh `Rclone(...)`
per test - each trying to initialize its own runtime - would fail on the
second one. The runtime is initialized once with `build_do_spaces_config()`'s
`[dst]` remote; every test in the suite that needs a client shares that same
already-configured remote by name rather than supplying its own config.

`test_s3.py` builds `S3Credentials`/`S3Client` directly and never
constructs an `Rclone`, so it needs neither fixture.
"""

import os
from collections.abc import Iterator

import pytest

from helpers import DIGITAL_OCEAN_SPACES_ENV_VARS, missing_cloud_env_vars
from rclone_kit import Config, Rclone
from rclone_kit.native.library import resolve_library_path
from rclone_kit.native.runtime import RcloneRuntime

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


@pytest.fixture(scope="session")
def cloud_runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RcloneRuntime]:
    """One `RcloneRuntime`, initialized exactly once for the whole
    `tests/cloud` session with the shared `[dst]` DigitalOcean Spaces
    remote. Skips (via `build_do_spaces_config`) when credentials are
    missing, before any native initialization is attempted.
    """
    config = build_do_spaces_config()
    config_path = tmp_path_factory.mktemp("cloud-session-runtime") / "rclone.conf"
    config_path.write_text(config.text, encoding="utf-8")
    rt = RcloneRuntime.from_library_path(resolve_library_path(None))
    rt.initialize(config_path=config_path)
    yield rt
    rt.close()


@pytest.fixture
def cloud_rclone(cloud_runtime: RcloneRuntime) -> Rclone:
    """An `Rclone` client sharing the session's one initialized runtime and
    its already-configured `dst:` remote."""
    return Rclone(None, runtime=cloud_runtime)
