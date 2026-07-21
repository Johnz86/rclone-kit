"""Shared gating and fixtures for `tests/live/`.

This suite exercises the real `rclone-kit` implementation against a live
Ceph/S3 backend, using an actual rclone config file at the repository root
(`rclone-test.conf`, gitignored, never committed - see `rclone*.conf` in
`.gitignore`). It is intentionally separate from `tests/cloud/`, which runs
against DigitalOcean Spaces / Backblaze B2 via environment variables.

Two rules keep this suite from running by accident:

1. Every test here carries the `live` marker (`pytestmark = pytest.mark.live`
   in each module). `pytest_collection_modifyitems` below deselects those
   tests unless the caller explicitly asked for them with `-m live` - a bare
   `pytest` run (which would otherwise sweep this directory in via
   `testpaths`) collects zero live tests.
2. Once `-m live` is explicitly requested, the suite requires
   `rclone-test.conf` to exist. If it does not, the whole session is stopped
   with `pytest.exit()` and a message describing the file to create, rather
   than letting every test fail or skip individually.

All test data lives under `LIVE_TEST_ROOT`, a bucket dedicated to this
suite. Nothing outside that bucket is ever written to or deleted.
"""

import uuid
from pathlib import Path
from typing import Generator

import pytest

from rclone_kit import Rclone

LIVE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "rclone-test.conf"
LIVE_REMOTE = "kinit-s3"
LIVE_TEST_BUCKET = "rclone-kit-live-test"
LIVE_TEST_ROOT = f"{LIVE_REMOTE}:{LIVE_TEST_BUCKET}"

_MISSING_CONFIG_HINT = f"""
tests/live requires a real rclone config file that is not present:
    {LIVE_CONFIG_PATH}

This file is gitignored and never committed (see `rclone*.conf` in
.gitignore). Create it with a `[{LIVE_REMOTE}]` remote, for example:

    [{LIVE_REMOTE}]
    type = s3
    provider = Ceph
    access_key_id = <your access key>
    secret_access_key = <your secret key>
    endpoint = <your endpoint URL>

The suite reads and writes real data, scoped to `{LIVE_TEST_ROOT}/`. That
bucket is created automatically before the first test runs if it does not
already exist. No other bucket on the remote is ever modified.
"""


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    live_items = [item for item in items if item.get_closest_marker("live")]
    if not live_items:
        return

    markexpr = getattr(config.option, "markexpr", "") or ""
    if "live" not in markexpr:
        items[:] = [item for item in items if item not in live_items]
        config.hook.pytest_deselected(items=live_items)
        return

    if not LIVE_CONFIG_PATH.is_file():
        pytest.exit(_MISSING_CONFIG_HINT, returncode=1)


@pytest.fixture(scope="session")
def live_rclone() -> Rclone:
    return Rclone(LIVE_CONFIG_PATH)


@pytest.fixture(scope="session", autouse=True)
def _ensure_live_bucket(live_rclone: Rclone) -> None:
    """Idempotently create `LIVE_TEST_BUCKET` before any test runs.

    Unlike AWS S3, this Ceph cluster does not auto-create a bucket on the
    first `PutObject` - every write into a missing bucket fails with
    `NoSuchBucket`. `rclone mkdir` is idempotent, so running it once per
    session up front is cheap and safe even when the bucket already exists.
    There is no public `Rclone` method for this, so it goes through the
    same private `_run` hook `tests/helpers.py` already documents as the
    sanctioned way for tests to reach the backend directly.
    """
    live_rclone._run(["mkdir", LIVE_TEST_ROOT], check=True)


@pytest.fixture
def live_test_prefix(live_rclone: Rclone) -> Generator[str]:
    """A unique, disposable path under `LIVE_TEST_ROOT` for one test.

    Purged on teardown regardless of what the test left behind, so a failed
    assertion never leaks scoped test data.
    """
    prefix = f"{LIVE_TEST_ROOT}/{uuid.uuid4().hex}"
    yield prefix
    live_rclone.purge(prefix)


@pytest.fixture
def local_source_tree(tmp_path: Path) -> Path:
    """A small local directory tree for copy/diff/walk tests to transfer."""
    root = tmp_path / "source"
    (root / "nested").mkdir(parents=True)
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("bravo")
    (root / "nested" / "c.txt").write_text("charlie")
    return root
