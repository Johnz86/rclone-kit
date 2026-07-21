"""Shared gating and fixtures for `tests/live/gdrive/`.

This suite exercises the real `rclone-kit` implementation against a real
Google Drive remote, using an actual rclone config file at the repository
root (`rclone-gdrive.conf`, gitignored, never committed - see `rclone*.conf`
in `.gitignore`). It is a sibling of `tests/live/s3/` (which covers a real
S3-compatible/Ceph backend), not an extension of it: Drive has no bucket
concept, different rate limits, and its own folder semantics, so it is the
better test of the claim that rclone-kit's general API is backend-agnostic.
Each real-backend suite under `tests/live/` is fully self-contained (its own
marker, its own config file, its own fixtures) so any one of them can run
independently of the others as more providers are added.

Two rules keep this suite from running by accident, mirroring
`tests/live/s3/conftest.py` exactly:

1. Every test here carries the `live_gdrive` marker (`pytestmark =
   pytest.mark.live_gdrive` in each module). `pytest_collection_modifyitems`
   below deselects those tests unless the caller explicitly asked for them
   with `-m live_gdrive` - a bare `pytest` run (which would otherwise sweep
   this directory in via `testpaths`) collects zero live_gdrive tests.
2. Once `-m live_gdrive` is explicitly requested, the suite requires
   `rclone-gdrive.conf` to exist. If it does not, the whole session is
   stopped with `pytest.exit()` and a message describing the file to
   create, rather than letting every test fail or skip individually.

This suite has its own marker and config file rather than joining
`tests/live/s3`'s gate, so a contributor with only one of the two backends
configured can still run that one's suite independently.

Unlike the Ceph/S3 backend `tests/live/s3` covers, Google Drive auto-creates
a missing folder on first write (verified directly: `write_text()` into a
never-before-seen `LIVE_TEST_ROOT` subpath succeeds with no prior `mkdir`
step), so there is no equivalent of `tests/live/s3`'s `_ensure_live_bucket`
fixture here - it would have nothing to do.

All test data lives under `LIVE_TEST_ROOT`, a folder dedicated to this
suite. Nothing outside that folder is ever written to or deleted.

Test files in this directory must reach `LIVE_REMOTE`/`LIVE_TEST_ROOT`
through the `live_remote_name`/`live_test_root` fixtures below, never via
`from conftest import ...` - see the matching note in
`tests/live/s3/conftest.py` for why: this module and that one are both
literally named `conftest`, and a bare import of either resolves through
Python's global `sys.modules["conftest"]` cache rather than pytest's own
per-directory conftest resolution once both are loaded in one session
(which a bare `pytest` run does for collection alone, even if both suites
are later deselected).
"""

import uuid
from collections.abc import Generator
from pathlib import Path

import pytest

from rclone_kit import Rclone

LIVE_CONFIG_PATH = Path(__file__).resolve().parents[3] / "rclone-gdrive.conf"
LIVE_REMOTE = "gdrive"
LIVE_TEST_FOLDER = "rclone-kit-live-test"
LIVE_TEST_ROOT = f"{LIVE_REMOTE}:{LIVE_TEST_FOLDER}"

_MISSING_CONFIG_HINT = f"""
tests/live/gdrive requires a real rclone config file that is not present:
    {LIVE_CONFIG_PATH}

This file is gitignored and never committed (see `rclone*.conf` in
.gitignore). Create it with a `[{LIVE_REMOTE}]` remote by running (PowerShell
needs the `&` call operator for a quoted executable path):

    & "<path-to-rclone.exe>" config create {LIVE_REMOTE} drive --config rclone-gdrive.conf

This opens a browser for the Google OAuth consent screen. For anything
beyond light manual testing, pass your own OAuth `client_id`/`client_secret`
(Google Cloud Console) as extra arguments to avoid rclone's shared default
client's rate limits.

The suite reads and writes real data, scoped to `{LIVE_TEST_ROOT}/`. Drive
creates that folder automatically on first write; no other file or folder
on the remote is ever modified.
"""


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    live_items = [item for item in items if item.get_closest_marker("live_gdrive")]
    if not live_items:
        return

    markexpr = getattr(config.option, "markexpr", "") or ""
    if "live_gdrive" not in markexpr:
        items[:] = [item for item in items if item not in live_items]
        config.hook.pytest_deselected(items=live_items)
        return

    if not LIVE_CONFIG_PATH.is_file():
        pytest.exit(_MISSING_CONFIG_HINT, returncode=1)


@pytest.fixture(scope="session")
def live_rclone() -> Rclone:
    return Rclone(LIVE_CONFIG_PATH)


@pytest.fixture
def live_remote_name() -> str:
    return LIVE_REMOTE


@pytest.fixture
def live_test_root() -> str:
    return LIVE_TEST_ROOT


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
