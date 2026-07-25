"""Unit tests for `rclone_kit.util`'s remote/path splitting (finding #6,
"Linux path-modeling bugs": a bare local path - most commonly a Unix
absolute path, which never contains a colon - was previously misparsed as
a remote whose name is the entire path, losing the path itself).
"""

from typing import cast

from rclone_kit.client import Rclone
from rclone_kit.dir import Dir
from rclone_kit.remote import Remote
from rclone_kit.util import split_remote_name_and_path, to_path

_FAKE_RCLONE = cast(Rclone, object())


def test_split_remote_name_and_path_with_a_real_remote() -> None:
    assert split_remote_name_and_path("remote:bucket/prefix") == ("remote", "bucket/prefix")


def test_split_remote_name_and_path_with_a_bare_remote_root() -> None:
    assert split_remote_name_and_path("remote:") == ("remote", "")


def test_split_remote_name_and_path_with_extra_colons_in_the_path() -> None:
    # Matches the pre-existing (unchanged) colon-naive behavior for a path
    # segment that itself contains a colon.
    assert split_remote_name_and_path("remote:a:b") == ("remote", "a:b")


def test_split_remote_name_and_path_with_a_colonless_unix_local_path() -> None:
    assert split_remote_name_and_path("/srv/data") == ("", "/srv/data")


def test_to_path_with_a_unix_local_path_round_trips_through_str() -> None:
    rpath = to_path("/srv/data", _FAKE_RCLONE)

    assert rpath.remote.name == ""
    assert rpath.path == "/srv/data"
    assert str(rpath) == "/srv/data"


def test_to_path_with_a_remote_path_still_splits_normally() -> None:
    rpath = to_path("remote:bucket/prefix", _FAKE_RCLONE)

    assert rpath.remote.name == "remote"
    assert rpath.path == "bucket/prefix"
    assert str(rpath) == "remote:bucket/prefix"


def test_dir_built_from_a_unix_local_path_reconstructs_without_a_stray_colon() -> None:
    dir_obj = Dir(to_path("/srv/data", _FAKE_RCLONE))

    assert str(dir_obj) == "/srv/data"


def test_dir_built_from_a_remote_object_is_unaffected() -> None:
    # `Dir(Remote(...))`'s own "remote:remote:" duplication (passing
    # `str(remote)`, itself already colon-suffixed, into `RPath.path`) is a
    # separate, pre-existing quirk unrelated to finding #6 - this only
    # confirms the RPath.__str__ fix above did not change it further, since
    # `remote.name` is non-empty here.
    remote = Remote(name="remote", rclone=_FAKE_RCLONE)

    dir_obj = Dir(remote)

    assert str(dir_obj) == "remote:remote:"
