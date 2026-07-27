"""Value semantics of the domain path types.

`RPath`, `Remote`, `RealFS`, `RemoteFS` and `FSPath` all denote *a place*,
not *an object*. Before these tests they inherited `object`'s identity
equality, which silently broke every set, dict and dedupe built on them -
`DirListing._dedupe` could never filter anything, and two `FSPath`s for
the same local file compared unequal.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from rclone_kit.access import ListingAccess
from rclone_kit.dir_listing import DirListing
from rclone_kit.fs.filesystem import FSPath, RealFS, RemoteFS, RemoteFSAccess
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath

_REMOTE_NAME = "dst"
_OTHER_REMOTE_NAME = "src"
_ENTRY_PATH = "bucket/dir/file.txt"
_ENTRY_NAME = "file.txt"
_ENTRY_SIZE = 1234
_ENTRY_MIME_TYPE = "text/plain"
_ENTRY_MOD_TIME = "2024-01-01T00:00:00Z"

_REMOTE_FS_SRC = "dst:bucket"
_OTHER_REMOTE_FS_SRC = "dst:other-bucket"
# Never touched on disk: every test here is pure path arithmetic.
_LOCAL_PATH = Path("rclone-kit-value-semantics") / _ENTRY_NAME


def _fake_listing_access() -> ListingAccess:
    """A fresh stand-in client, distinct from every other one.

    Never called: these tests exist precisely to show that which client a
    value is bound to does not change the value.
    """
    return cast(ListingAccess, object())


def _fake_remote_fs_access() -> RemoteFSAccess:
    """A fresh stand-in client for `RemoteFS`, never called."""
    return cast(RemoteFSAccess, object())


@dataclass(frozen=True)
class RPathValueCase:
    """A complete set of `RPath` value fields.

    Defaults spell out the baseline entry, so each case constant states
    only the field it changes and its name says which one that is.
    """

    remote_name: str = _REMOTE_NAME
    path: str = _ENTRY_PATH
    name: str = _ENTRY_NAME
    size: int = _ENTRY_SIZE
    mime_type: str = _ENTRY_MIME_TYPE
    mod_time: str = _ENTRY_MOD_TIME
    is_dir: bool = False


def _rpath(case: RPathValueCase, rclone: ListingAccess | None = None) -> RPath:
    """Build the `RPath` a case describes, wired to `rclone` (or, by
    default, to a client unique to this call)."""
    client = _fake_listing_access() if rclone is None else rclone
    rpath = RPath(
        remote=Remote(case.remote_name, client),
        path=case.path,
        name=case.name,
        size=case.size,
        mime_type=case.mime_type,
        mod_time=case.mod_time,
        is_dir=case.is_dir,
    )
    rpath.set_rclone(client)
    return rpath


BASELINE_ENTRY = RPathValueCase()
ENTRY_ON_ANOTHER_REMOTE = RPathValueCase(remote_name=_OTHER_REMOTE_NAME)
ENTRY_AT_ANOTHER_PATH = RPathValueCase(path="bucket/dir/other.txt")
ENTRY_WITH_ANOTHER_NAME = RPathValueCase(name="other.txt")
ENTRY_WITH_ANOTHER_SIZE = RPathValueCase(size=_ENTRY_SIZE + 1)
ENTRY_WITH_ANOTHER_MIME_TYPE = RPathValueCase(mime_type="application/json")
ENTRY_WITH_ANOTHER_MOD_TIME = RPathValueCase(mod_time="2025-06-01T12:00:00Z")
ENTRY_AS_DIRECTORY = RPathValueCase(is_dir=True)

ENTRY_WITH_TRAILING_SLASH = RPathValueCase(path=f"{_ENTRY_PATH}/")

DISTINCT_FROM_BASELINE_CASES = [
    ENTRY_ON_ANOTHER_REMOTE,
    ENTRY_AT_ANOTHER_PATH,
    ENTRY_WITH_ANOTHER_NAME,
    ENTRY_WITH_ANOTHER_SIZE,
    ENTRY_WITH_ANOTHER_MIME_TYPE,
    ENTRY_WITH_ANOTHER_MOD_TIME,
    ENTRY_AS_DIRECTORY,
]


@pytest.mark.parametrize(
    "case",
    DISTINCT_FROM_BASELINE_CASES,
    ids=[
        "entry_on_another_remote",
        "entry_at_another_path",
        "entry_with_another_name",
        "entry_with_another_size",
        "entry_with_another_mime_type",
        "entry_with_another_mod_time",
        "entry_as_directory",
    ],
)
def test_every_listed_field_participates_in_rpath_identity(case: RPathValueCase) -> None:
    baseline = _rpath(BASELINE_ENTRY)
    variant = _rpath(case)

    assert baseline != variant
    assert len({baseline, variant}) == 2


def test_rpaths_with_identical_fields_are_equal_and_hash_equal() -> None:
    first = _rpath(BASELINE_ENTRY)
    second = _rpath(BASELINE_ENTRY)

    assert first == second
    assert hash(first) == hash(second)
    assert len({first, second}) == 1


def test_rpath_identity_ignores_the_rclone_back_reference() -> None:
    """The bound client is how you *act* on a path, not which path it is."""
    one_client = _fake_listing_access()
    another_client = _fake_listing_access()

    assert _rpath(BASELINE_ENTRY, one_client) == _rpath(BASELINE_ENTRY, another_client)
    assert hash(_rpath(BASELINE_ENTRY, one_client)) == hash(_rpath(BASELINE_ENTRY, another_client))


def test_rpath_identity_survives_setting_the_back_reference_afterwards() -> None:
    unbound = _rpath(BASELINE_ENTRY)
    bound = _rpath(BASELINE_ENTRY)
    unbound.set_rclone(None)

    assert unbound == bound
    assert hash(unbound) == hash(bound)


def test_rpath_normalises_a_trailing_slash_before_comparing() -> None:
    assert _rpath(ENTRY_WITH_TRAILING_SLASH) == _rpath(BASELINE_ENTRY)


def test_rpath_is_never_equal_to_a_non_rpath() -> None:
    assert _rpath(BASELINE_ENTRY) != _ENTRY_PATH


def test_remote_identity_is_its_name_not_its_client() -> None:
    first = Remote(_REMOTE_NAME, _fake_listing_access())
    second = Remote(_REMOTE_NAME, _fake_listing_access())

    assert first == second
    assert hash(first) == hash(second)


def test_remotes_with_different_names_are_different_values() -> None:
    first = Remote(_REMOTE_NAME, _fake_listing_access())
    second = Remote(_OTHER_REMOTE_NAME, _fake_listing_access())

    assert first != second


def test_dir_listing_collapses_duplicate_file_entries() -> None:
    duplicate = _rpath(BASELINE_ENTRY)

    with pytest.warns(UserWarning, match=re.escape(str(duplicate))):
        listing = DirListing([_rpath(BASELINE_ENTRY), duplicate])

    assert len(listing.files) == 1


def test_dir_listing_collapses_duplicate_dir_entries() -> None:
    with pytest.warns(UserWarning):
        listing = DirListing([_rpath(ENTRY_AS_DIRECTORY), _rpath(ENTRY_AS_DIRECTORY)])

    assert len(listing.dirs) == 1


def test_dir_listing_keeps_entries_that_disagree_on_metadata() -> None:
    """Same path, different size: a listing anomaly the caller must still
    see, not a repeat that can be collapsed."""
    listing = DirListing([_rpath(BASELINE_ENTRY), _rpath(ENTRY_WITH_ANOTHER_SIZE)])

    assert len(listing.files) == 2


def test_real_fs_instances_are_interchangeable() -> None:
    assert RealFS() == RealFS()
    assert hash(RealFS()) == hash(RealFS())


def test_real_fs_is_never_equal_to_a_remote_fs() -> None:
    remote_fs = RemoteFS(_fake_remote_fs_access(), _REMOTE_FS_SRC)

    assert RealFS() != remote_fs
    assert remote_fs != RealFS()


def test_remote_fs_instances_over_the_same_client_and_src_are_equal() -> None:
    access = _fake_remote_fs_access()

    first = RemoteFS(access, _REMOTE_FS_SRC)
    second = RemoteFS(access, _REMOTE_FS_SRC)

    assert first == second
    assert hash(first) == hash(second)


def test_remote_fs_differs_when_the_src_root_differs() -> None:
    access = _fake_remote_fs_access()

    assert RemoteFS(access, _REMOTE_FS_SRC) != RemoteFS(access, _OTHER_REMOTE_FS_SRC)


def test_remote_fs_differs_when_the_bound_client_differs() -> None:
    first = RemoteFS(_fake_remote_fs_access(), _REMOTE_FS_SRC)
    second = RemoteFS(_fake_remote_fs_access(), _REMOTE_FS_SRC)

    assert first != second


def test_real_fs_backed_fspath_round_trips_through_a_set() -> None:
    """`RealFS.from_path` mints a new `RealFS` per call, so this is the
    exact case the old identity-based hash got wrong."""
    first = FSPath.from_path(_LOCAL_PATH)
    second = FSPath.from_path(_LOCAL_PATH)

    assert first == second
    assert hash(first) == hash(second)
    assert second in {first}
    assert len({first, second}) == 1


def test_remote_fs_backed_fspath_round_trips_through_a_set() -> None:
    access = _fake_remote_fs_access()

    first = RemoteFS(access, _REMOTE_FS_SRC).get_path(_ENTRY_PATH)
    second = RemoteFS(access, _REMOTE_FS_SRC).get_path(_ENTRY_PATH)

    assert first == second
    assert hash(first) == hash(second)
    assert second in {first}
    assert len({first, second}) == 1


def test_fspaths_with_the_same_path_on_different_filesystems_are_not_equal() -> None:
    local = FSPath(RealFS(), _ENTRY_PATH)
    remote = RemoteFS(_fake_remote_fs_access(), _REMOTE_FS_SRC).get_path(_ENTRY_PATH)

    assert local != remote


def test_fspath_is_never_equal_to_a_non_fspath() -> None:
    assert FSPath(RealFS(), _ENTRY_PATH) != _ENTRY_PATH


def test_fspath_children_of_equal_parents_are_equal() -> None:
    """`__truediv__` rebuilds an `FSPath` around the same `FS`; the child
    must stay usable as a key, which `fs_walk` relies on."""
    first = FSPath.from_path(_LOCAL_PATH).parent / _ENTRY_NAME
    second = FSPath.from_path(_LOCAL_PATH).parent / _ENTRY_NAME

    assert first == second
    assert len({first, second}) == 1
