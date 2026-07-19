"""Unit tests for `tests/cloud/conftest.py`'s `build_do_spaces_config`.

Offline and credential-free: uses `monkeypatch` to simulate the
environment, so it belongs in the default `tests/cloud` collection without
requiring `@pytest.mark.cloud` or real DigitalOcean Spaces access.
"""

import pytest
from conftest import build_do_spaces_config

from helpers import DIGITAL_OCEAN_SPACES_ENV_VARS


def test_do_spaces_config_skips_when_env_vars_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in DIGITAL_OCEAN_SPACES_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(pytest.skip.Exception):
        build_do_spaces_config()


def test_do_spaces_config_builds_expected_config_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """BUCKET_URL is required by the skip check (it's part of
    DIGITAL_OCEAN_SPACES_ENV_VARS) but the fixture hardcodes the actual
    endpoint used, matching every _generate_rclone_config() it replaces -
    so its value here is irrelevant, only its presence.
    """
    monkeypatch.setenv("BUCKET_NAME", "my-bucket")
    monkeypatch.setenv("BUCKET_KEY_SECRET", "secret-value")
    monkeypatch.setenv("BUCKET_KEY_PUBLIC", "public-key")
    monkeypatch.setenv("BUCKET_URL", "unused-value")

    config = build_do_spaces_config()

    parsed = config.parse()
    section = parsed.sections["dst"]
    assert section.type() == "s3"
    assert section.access_key_id() == "public-key"
    assert section.secret_access_key() == "secret-value"
    assert section.endpoint() == "sfo3.digitaloceanspaces.com"
