"""Shared gating and fixtures for `tests/live/gdrive_authorization/`.

This suite exercises `rclone-kit`'s own OAuth authorization flow
(`Rclone.authorize()`) against a real Google Drive account - the process of
*becoming* configured, not general operations against an already-configured
remote (that's `tests/live/gdrive`'s job). It is a sibling of
`tests/live/gdrive/` and `tests/live/s3/`, not an extension of either: same
self-contained-suite shape (own marker, own config file, own fixtures), but
testing something structurally different enough that it needs its own
directory - see "Why not `tests/live/gdrive`" below.

Two rules keep this suite from running by accident, mirroring
`tests/live/s3/conftest.py`/`tests/live/gdrive/conftest.py` exactly:

1. Every test here carries the `live_gdrive_authorization` marker.
   `pytest_collection_modifyitems` below deselects those tests unless the
   caller explicitly asked for them with `-m live_gdrive_authorization` - a
   bare `pytest` run (which would otherwise sweep this directory in via
   `testpaths`) collects zero tests from it.
2. Once `-m live_gdrive_authorization` is explicitly requested, the suite
   requires a built native library (see `_local_build_library_path()`). If
   none is found, the whole session is stopped with `pytest.exit()` and a
   message describing the build command, rather than letting every test
   fail individually.

A third thing this suite needs that the other two don't: a real human
present. `authorized_remote` below blocks mid-fixture-setup waiting for a
person to approve access in a real browser - unlike `live_gdrive`/`live_s3`,
which only need a config file to already exist and then run fully
unattended. This cannot be scripted further without violating the
provider's terms of service; never run this suite automatically in CI.

Why not `tests/live/gdrive`: mixing "drive an OAuth consent flow" into that
suite would mean every one of its otherwise-unattended tests could suddenly
block on human interaction depending on collection order, and its
`LIVE_CONFIG_PATH`/`LIVE_REMOTE` assume a remote that is *already*
authorized out-of-band - the opposite of what this suite tests. Using a
different marker, a different config file (`rclone-gdrive-authtest.conf`,
not `rclone-gdrive.conf`), and a different remote name (`gdrive-authtest`,
not `gdrive`) keeps the two suites from ever touching the same file, remote,
or fixture, so `-m live_gdrive` and `-m live_gdrive_authorization` can each
be run independently - or together - without interfering with each other.

No Google Cloud Console setup needed: like `scripts/verify_gdrive_authorization.py`
(a manual, non-pytest equivalent of this suite for a quick one-off check),
this uses rclone's own built-in shared client_id via `Rclone.authorize()`'s
local-direct mode (no `public_callback_url`, no `client_id`) - see
`AuthorizationRequest`'s docstring. `scope` is `drive.readonly`: this suite
only needs to prove the authorization flow itself works, not exercise
general read/write operations.

The config file this suite authorizes into is gitignored (matches the
`rclone*.conf` pattern in `.gitignore`) and, once a session succeeds,
contains a real access/refresh token. It is deliberately *not* deleted at
teardown - unlike `live_test_prefix`'s scoped remote-data cleanup in the
other two suites - so a later run can reuse it via `RemoteConflictPolicy`
defensive recreation below instead of needing a fresh human approval every
time. Delete it by hand if you don't want it lying around.
"""

from __future__ import annotations

import contextlib
import platform
import webbrowser
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from rclone_kit import Rclone, shared_runtime
from rclone_kit.rc.client import RcClient
from rclone_kit.runtime.exceptions import UnsupportedPlatformError
from rclone_kit.runtime.native_platform import resolve_native_target

LIVE_CONFIG_PATH = Path(__file__).resolve().parents[3] / "rclone-gdrive-authtest.conf"
LIVE_REMOTE = "gdrive-authtest"
_CONSENT_TIMEOUT_SECONDS = 300.0

_MISSING_LIBRARY_HINT = """
tests/live/gdrive_authorization requires a native library built locally
(unlike tests/live/gdrive and tests/live/s3, which only need a config file,
this suite drives the embedded runtime directly and needs the same local
dev build tests/native/ uses):

    uv run python scripts/native/build.py
"""


def _local_build_library_path() -> Path | None:
    """The library `scripts/native/build.py` produces, at the same path
    `tests/native/conftest.py` looks for it. Duplicated rather than
    imported from there: this directory's own conftest.py must stay
    self-contained, matching every other suite under `tests/live/`, and a
    cross-suite `conftest` import hits the exact `sys.modules["conftest"]`
    collision this module's own docstring (and its siblings') warns about.
    """
    try:
        target = resolve_native_target(system=platform.system(), machine=platform.machine())
    except UnsupportedPlatformError:
        return None
    candidate = (
        Path(__file__).resolve().parents[3]
        / "build"
        / "native"
        / f"{target.operating_system.value}-{target.architecture.value}"
        / target.library_filename
    )
    return candidate if candidate.is_file() else None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    live_items = [item for item in items if item.get_closest_marker("live_gdrive_authorization")]
    if not live_items:
        return

    markexpr = getattr(config.option, "markexpr", "") or ""
    if "live_gdrive_authorization" not in markexpr:
        items[:] = [item for item in items if item not in live_items]
        config.hook.pytest_deselected(items=live_items)
        return

    if _local_build_library_path() is None:
        pytest.exit(_MISSING_LIBRARY_HINT, returncode=1)


@pytest.fixture(scope="session")
def live_rclone() -> Iterator[Rclone]:
    """Authorizes `LIVE_REMOTE` exactly once per test session - the one
    point in this suite that blocks on a human approving access in a real
    browser - then returns a client built fresh from the resulting config,
    matching the documented stale-`self.config`-snapshot caveat
    (`docs/production_usage.md`, "Authorizing a remote through rclone's own
    OAuth flow"): the client `authorize()` was called on must not be the
    one other tests read from.
    """
    library_path = _local_build_library_path()
    assert library_path is not None  # pytest_collection_modifyitems already checked this

    runtime = shared_runtime(library_path=library_path, config_path=LIVE_CONFIG_PATH)
    rc_client = RcClient(runtime)
    # Defensive: a `RemoteConflictPolicy.RECONNECT` re-authorization would
    # need the remote to already exist (`config/update` errors otherwise -
    # see AuthorizationManager's own module docstring); deleting first and
    # always creating fresh sidesteps that entirely, so this fixture behaves
    # identically whether or not a previous session already authorized this
    # remote.
    with contextlib.suppress(Exception):
        rc_client.call("config/delete", name=LIVE_REMOTE)

    authorizing_client = Rclone(None, runtime=runtime)
    try:
        session = authorizing_client.authorize(
            remote_name=LIVE_REMOTE,
            backend="drive",
            backend_options={
                "scope": "drive.readonly",
                # Drive's Config() asks a follow-up question after OAuth
                # completes that isn't part of the fixed OAuth question
                # policy - pre-answered per
                # docs/rclone_authorization_design.md's "Provider
                # application credentials", matching its own "No" default.
                "config_change_team_drive": "false",
            },
            expires_in=timedelta(seconds=_CONSENT_TIMEOUT_SECONDS),
        )
        try:
            url = session.authorization_url
            print(
                f"\n[live_gdrive_authorization] Opening {url}\n"
                "[live_gdrive_authorization] Approve access in the browser that opens."
            )
            webbrowser.open(url)
            session.wait(timeout=_CONSENT_TIMEOUT_SECONDS)
        finally:
            session.close()
    finally:
        authorizing_client.close()

    rclone = Rclone(LIVE_CONFIG_PATH, runtime=runtime)
    yield rclone
    rclone.close()


@pytest.fixture
def live_remote_name() -> str:
    return LIVE_REMOTE
