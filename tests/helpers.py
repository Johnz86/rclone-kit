"""Shared constants and skip logic for the integration and cloud test suites.

`tests/` is on `sys.path` for the whole session (see `pythonpath` in
`pyproject.toml`), so any test file imports this module with
`from helpers import ...` regardless of which suite directory it lives in.
"""

import os
import unittest
from collections.abc import Sequence

CLOUD_TEST_KEY_PREFIX = "rclone-kit-test/"
CLOUD_TEST_REMOTE_ROOT = "dst:rclone-kit-unit-test"

DIGITAL_OCEAN_SPACES_ENV_VARS: tuple[str, ...] = (
    "BUCKET_NAME",
    "BUCKET_KEY_SECRET",
    "BUCKET_KEY_PUBLIC",
    "BUCKET_URL",
)

BACKBLAZE_B2_ENV_VARS: tuple[str, ...] = (
    "B2_BUCKET_NAME",
    "B2_ACCESS_KEY_ID",
    "B2_SECRET_ACCESS_KEY",
    "B2_ENDPOINT_URL",
)


def missing_cloud_env_vars(required_vars: Sequence[str]) -> list[str]:
    """Return the subset of `required_vars` that are unset or empty."""
    return [var for var in required_vars if not os.getenv(var)]


def skip_if_missing_cloud_env(
    testcase: unittest.TestCase,
    required_vars: Sequence[str] = DIGITAL_OCEAN_SPACES_ENV_VARS,
) -> None:
    """Skip `testcase` unless every variable in `required_vars` is set.

    The skip message names the missing variables by name. It never reads or
    prints their values.
    """
    missing = missing_cloud_env_vars(required_vars)
    if missing:
        testcase.skipTest(f"Missing required environment variables: {', '.join(missing)}")
