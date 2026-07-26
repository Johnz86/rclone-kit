"""Manual, one-off verification that `rclone-kit`'s own authorization flow
(`AuthorizationManager`/`Rclone.authorize`) can authorize a real Google
Drive remote against a personal account - not a pytest test, and not part
of the installed package. Run this by hand once to visually confirm the
feature works end to end against a real provider; it is not meant to be
part of any automated suite (see `docs/rclone_authorization_design.md`'s
"Live tests" section for why: it needs a real human to click "Allow" in a
real browser).

No Google Cloud Console setup, no client_id/client_secret: this uses
rclone's own built-in shared client_id, the same one plain interactive
`rclone config create gdrive drive` falls back to - `Rclone.authorize()`
pre-answers the "continue with the shared client_id?" confirmation exactly
the way a human typing "y" at that prompt would (see
`AuthorizationRequest`'s docstring). `public_callback_url` is also left
unset, since this is the "local direct" case: the browser and this script
run on the same machine, so `session.authorization_url` is simply rclone's
own local listener URL, opened directly - no relay server needed either.

Run:

    uv run python scripts/verify_gdrive_authorization.py

The config this writes (`rclone-gdrive-authtest.conf` at the repo root) is
gitignored (matches the `rclone*.conf` pattern already in `.gitignore`) and
ends up containing a real access/refresh token - delete it when done if you
don't want it lying around. The `scope` requested is `drive.readonly`, not
full `drive` access, since this script only needs to prove the flow works
and list a folder.
"""

from __future__ import annotations

import platform
import sys
import webbrowser
from pathlib import Path

from rclone_kit import Rclone, shared_runtime
from rclone_kit.runtime.exceptions import UnsupportedPlatformError
from rclone_kit.runtime.native_platform import resolve_native_target

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "rclone-gdrive-authtest.conf"
_REMOTE_NAME = "gdrive-authtest"
_CONSENT_TIMEOUT_SECONDS = 300.0
_MAX_LISTED_ENTRIES = 10

_MISSING_LIBRARY_HINT = """
No local native library build found under build/native/.

Build it first:

    uv run python scripts/native/build.py

Or, if you're running against an installed wheel instead of a source
checkout, unset RCLONE_KIT_LIBRARY handling below (it's only needed for a
local dev build) - resolve_library_path() will find the packaged asset on
its own.
"""


def _local_build_library_path() -> Path | None:
    """The library `scripts/native/build.py` produces, at the same path
    `tests/native/conftest.py` looks for it - not resolvable automatically
    by `resolve_library_path()`, which only knows about an installed
    wheel's bundled asset, not this repo's own build output directory.
    """
    try:
        target = resolve_native_target(system=platform.system(), machine=platform.machine())
    except UnsupportedPlatformError:
        return None
    candidate = (
        _REPO_ROOT
        / "build"
        / "native"
        / f"{target.operating_system.value}-{target.architecture.value}"
        / target.library_filename
    )
    return candidate if candidate.is_file() else None


def main() -> int:
    library_path = _local_build_library_path()
    if library_path is None:
        print(_MISSING_LIBRARY_HINT, file=sys.stderr)
        return 1

    runtime = shared_runtime(library_path=library_path, config_path=_CONFIG_PATH)
    # rclone_conf=None: this client is only used to call authorize(), which
    # doesn't read self.config - the exists() check on _CONFIG_PATH would
    # otherwise fail since rclone hasn't written it yet.
    authorizing_client = Rclone(None, runtime=runtime)

    try:
        session = authorizing_client.authorize(
            remote_name=_REMOTE_NAME,
            backend="drive",
            backend_options={
                "scope": "drive.readonly",
                # Drive's Config() asks a follow-up question *after* OAuth
                # completes ("Configure this as a Shared Drive (Team
                # Drive)?", state "teamdrive_ok") that isn't part of the
                # fixed OAuth question policy - pre-answered here (matching
                # its own "No" default) per docs/rclone_authorization_design.md's
                # "Provider application credentials": a backend's extra
                # prompts must be pre-answered via parameters or added to
                # the driver's known-question table, never silently
                # skipped. A plain personal Drive has no Shared Drives to
                # pick between anyway.
                "config_change_team_drive": "false",
            },
        )
        try:
            url = session.authorization_url
            print(f"Opening {url}\nApprove access in the browser that opens.")
            webbrowser.open(url)
            result = session.wait(timeout=_CONSENT_TIMEOUT_SECONDS)
        finally:
            session.close()
    finally:
        authorizing_client.close()

    print(f"Authorized remote {result.remote_name!r}.")

    # A fresh client, not the one authorize() was called on: self.config is
    # a snapshot taken at construction time, so it must be built *after* the
    # session succeeded to see the new remote - see docs/production_usage.md,
    # "Authorizing a remote through rclone's own OAuth flow".
    verifying_client = Rclone(_CONFIG_PATH, runtime=runtime)
    listing = verifying_client.ls(f"{_REMOTE_NAME}:")
    print(
        f"Listing root of {_REMOTE_NAME}: "
        f"{len(listing.dirs)} folder(s), {len(listing.files)} file(s)"
    )
    for entry in (listing.dirs + listing.files)[:_MAX_LISTED_ENTRIES]:
        print(f"  {entry}")
    verifying_client.close()

    runtime.close()
    print(f"\nDone. Config written to {_CONFIG_PATH} (contains a real token - delete when done).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
