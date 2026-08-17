"""Live test file.

Proves `Rclone.authorize()` produces a genuinely usable Google Drive remote,
matching `docs/rclone_authorization_design.md`'s acceptance
criteria: the resulting remote can list a controlled folder, and its token
refreshes transparently using its refresh token. Both tests share the one
`live_rclone` fixture (`conftest.py`) so only one human browser-approval is
needed per test session, not one per test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from rclone_kit import DirListing, Rclone
from rclone_kit.rc.client import RcClient

pytestmark = pytest.mark.live_gdrive_authorization

_PAST_EXPIRY = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_authorized_remote_can_list_its_root(live_rclone: Rclone, live_remote_name: str) -> None:
    """Only a type/shape smoke check, mirroring
    `tests/live/gdrive/test_live_gdrive_ls_and_stat.py`'s own root-listing
    test: this is the real user's actual Drive, so its contents can't be
    asserted on without being flaky."""
    listing = live_rclone.ls(f"{live_remote_name}:", max_depth=0)

    assert isinstance(listing, DirListing)
    assert isinstance(listing.dirs, list)
    assert isinstance(listing.files, list)


def test_token_refreshes_transparently_after_forced_expiry(
    live_rclone: Rclone, live_remote_name: str
) -> None:
    """Forces the *stored* access token to look expired (keeping the real
    refresh token intact) via a direct RC `config/update`, then performs an
    ordinary operation - proving rclone's own `TokenSource` notices the
    expiry and silently mints a fresh access token from the refresh token,
    the same as it would after the token's real ~1 hour lifetime elapses.

    Edits the token through the RC layer, not the config file on disk:
    the file is not the runtime's authoritative state once loaded - editing
    it directly wouldn't touch the in-memory config the running native
    library actually uses for the next call.
    """
    assert live_rclone._embedded_runtime is not None
    rc_client = RcClient(live_rclone._embedded_runtime)

    current = rc_client.call("config/get", name=live_remote_name)
    token = json.loads(current["token"])
    original_access_token = token["access_token"]
    token["expiry"] = _PAST_EXPIRY

    rc_client.call(
        "config/update",
        name=live_remote_name,
        parameters={
            "token": json.dumps(token),
            # Pre-answered, matching conftest.py's fixture setup: skip the
            # replace-via-OAuth question (we're setting the token value
            # ourselves) and Drive's post-OAuth follow-up question.
            "config_refresh_token": "false",
            "config_change_team_drive": "false",
        },
        opt={"nonInteractive": True},
    )

    # If the refresh didn't happen, this fails with an auth error (an
    # expired access token, no automatic recovery) instead of listing
    # anything.
    listing = live_rclone.ls(f"{live_remote_name}:", max_depth=0)
    assert isinstance(listing, DirListing)

    refreshed = json.loads(rc_client.call("config/get", name=live_remote_name)["token"])
    assert refreshed["access_token"] != original_access_token
    assert refreshed["expiry"] > _PAST_EXPIRY
