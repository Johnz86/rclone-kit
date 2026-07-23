"""Unit tests for the embedded RC-backed listing/stat operations (CLI-to-
C-ABI migration ledger rows M02, L05, L08, L10).

Uses a fake `RcClient`-shaped object driven by canned per-method responses,
so these tests exercise request/response mapping without a built native
library. Native-DLL parity is covered by
`tests/native/test_listing_ops_embedded_integration.py`.
"""

import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.file import File
from rclone_kit.operations.config_ops import fetch_config_paths_embedded
from rclone_kit.operations.listing_ops_embedded import (
    check_exists_embedded,
    check_is_synced_embedded,
    fetch_listremotes_embedded,
    fetch_ls_embedded,
    fetch_size_file_embedded,
    fetch_stat_embedded,
)
from rclone_kit.remote import Remote
from rclone_kit.types import ListingOption, Order, SizeSuffix


class FakeRcClient:
    """A fake `RcClient` driven by one canned response per RC method."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, dict] = {}

    def call(self, method: str, **params: object) -> dict:
        self.calls.append((method, params))
        return self.responses[method]


class FakeAccess:
    """A structural `ListingAccess` stand-in.

    Only used so these tests satisfy `access`'s declared type; the functions
    under test here never call any of its methods, they only forward `self`
    into `Remote`/`RPath.set_rclone`.
    """

    def _run(
        self, cmd: list[str], check: bool = False, capture: bool | Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        raise NotImplementedError

    def ls(
        self,
        src: Dir | Remote | str | None = None,
        max_depth: int | None = None,
        glob: str | None = None,
        order: Order = Order.NORMAL,
        listing_option: ListingOption = ListingOption.ALL,
    ) -> DirListing:
        raise NotImplementedError

    def walk(
        self,
        src: Dir | Remote | str,
        max_depth: int = -1,
        breadth_first: bool = True,
        order: Order = Order.NORMAL,
    ) -> Generator[DirListing]:
        raise NotImplementedError

    def listremotes(self) -> list[Remote]:
        raise NotImplementedError

    def read_text(self, src: str) -> str:
        raise NotImplementedError

    def stat(self, src: str) -> File:
        raise NotImplementedError

    def size_file(self, src: str) -> SizeSuffix:
        raise NotImplementedError


_FILE_ITEM = {
    "Path": "path/to/object.txt",
    "Name": "object.txt",
    "Size": 5,
    "MimeType": "text/plain",
    "ModTime": "2024-01-01T00:00:00Z",
    "IsDir": False,
}

_DIR_ITEM = {
    "Path": "path/to",
    "Name": "to",
    "Size": 0,
    "MimeType": "inode/directory",
    "ModTime": "2024-01-01T00:00:00Z",
    "IsDir": True,
}


def test_fetch_listremotes_embedded_builds_remote_objects() -> None:
    client = FakeRcClient()
    client.responses["config/listremotes"] = {"remotes": ["alpha", "beta"]}

    remotes = fetch_listremotes_embedded(client, access=FakeAccess())

    assert [r.name for r in remotes] == ["alpha", "beta"]
    assert client.calls == [("config/listremotes", {})]


def test_fetch_listremotes_embedded_handles_no_remotes() -> None:
    client = FakeRcClient()
    client.responses["config/listremotes"] = {"remotes": []}

    assert fetch_listremotes_embedded(client, access=FakeAccess()) == []


def test_fetch_stat_embedded_returns_file_for_existing_item() -> None:
    client = FakeRcClient()
    client.responses["operations/stat"] = {"item": _FILE_ITEM}

    file = fetch_stat_embedded(client, access=FakeAccess(), src="remote:path/to/object.txt")

    assert file.name == "object.txt"
    assert file.size == 5
    assert client.calls == [("operations/stat", {"fs": "remote:path/to", "remote": "object.txt"})]


def test_fetch_stat_embedded_raises_file_not_found_when_item_is_null() -> None:
    client = FakeRcClient()
    client.responses["operations/stat"] = {"item": None}

    with pytest.raises(FileNotFoundError):
        fetch_stat_embedded(client, access=FakeAccess(), src="remote:missing.txt")


def test_fetch_stat_embedded_reports_directory_items() -> None:
    client = FakeRcClient()
    client.responses["operations/stat"] = {"item": _DIR_ITEM}

    file = fetch_stat_embedded(client, access=FakeAccess(), src="remote:path/to")

    assert file.path.is_dir


def test_fetch_size_file_embedded_requests_files_only() -> None:
    client = FakeRcClient()
    client.responses["operations/stat"] = {"item": _FILE_ITEM}

    size = fetch_size_file_embedded(client, access=FakeAccess(), src="remote:path/to/object.txt")

    assert size.as_int() == 5
    assert client.calls == [
        (
            "operations/stat",
            {"fs": "remote:path/to", "remote": "object.txt", "opt": {"filesOnly": True}},
        )
    ]


def test_fetch_size_file_embedded_raises_when_missing() -> None:
    client = FakeRcClient()
    client.responses["operations/stat"] = {"item": None}

    with pytest.raises(FileNotFoundError):
        fetch_size_file_embedded(client, access=FakeAccess(), src="remote:missing.txt")


def test_check_exists_embedded_true_when_item_present() -> None:
    client = FakeRcClient()
    client.responses["operations/stat"] = {"item": _FILE_ITEM}

    assert (
        check_exists_embedded(client, access=FakeAccess(), src="remote:path/to/object.txt") is True
    )


def test_check_exists_embedded_false_when_item_missing() -> None:
    client = FakeRcClient()
    client.responses["operations/stat"] = {"item": None}

    assert check_exists_embedded(client, access=FakeAccess(), src="remote:missing.txt") is False


def test_check_exists_embedded_true_for_empty_directory() -> None:
    """Unlike the CLI backend's `ls()`-based approximation, `operations/stat`
    reports an empty directory as existing - the ledger's sanctioned L10 fix.
    """
    client = FakeRcClient()
    client.responses["operations/stat"] = {"item": _DIR_ITEM}

    assert check_exists_embedded(client, access=FakeAccess(), src="remote:path/to") is True


def test_fetch_config_paths_embedded_returns_fixed_order() -> None:
    client = FakeRcClient()
    client.responses["config/paths"] = {
        "config": "/home/user/.config/rclone/rclone.conf",
        "cache": "/home/user/.cache/rclone",
        "temp": "/home/user/temp",
    }

    paths = fetch_config_paths_embedded(client)

    assert paths == [
        Path("/home/user/.config/rclone/rclone.conf"),
        Path("/home/user/.cache/rclone"),
        Path("/home/user/temp"),
    ]


def test_fetch_config_paths_embedded_omits_missing_values() -> None:
    client = FakeRcClient()
    client.responses["config/paths"] = {"config": "/home/user/rclone.conf"}

    paths = fetch_config_paths_embedded(client)

    assert paths == [Path("/home/user/rclone.conf")]


def test_stat_embedded_windows_drive_path_splits_parent_and_name() -> None:
    """A Windows drive path is not treated as a remote, and `fs` must be the
    containing directory (not the bare file itself, which rclone's local
    backend cannot open as a navigable root).
    """
    client = FakeRcClient()
    client.responses["operations/stat"] = {"item": _FILE_ITEM}

    fetch_stat_embedded(client, access=FakeAccess(), src="C:\\Users\\example\\object.txt")

    assert client.calls == [
        ("operations/stat", {"fs": "C:\\Users\\example\\", "remote": "object.txt"})
    ]


def test_fetch_listremotes_embedded_result_type_is_remote() -> None:
    client = FakeRcClient()
    client.responses["config/listremotes"] = {"remotes": ["alpha"]}

    remotes = fetch_listremotes_embedded(client, access=FakeAccess())

    assert isinstance(remotes[0], Remote)


def test_fetch_ls_embedded_with_no_src_lists_remotes_as_root_dirs() -> None:
    client = FakeRcClient()
    client.responses["config/listremotes"] = {"remotes": ["alpha", "beta"]}

    listing = fetch_ls_embedded(client, access=FakeAccess())

    assert isinstance(listing, DirListing)
    assert [d.remote.name for d in listing.dirs] == ["alpha", "beta"]
    assert all(d.path.path == "" for d in listing.dirs)
    assert client.calls == [("config/listremotes", {})]


def test_fetch_ls_embedded_non_recursive_by_default() -> None:
    client = FakeRcClient()
    client.responses["operations/list"] = {"list": [_FILE_ITEM, _DIR_ITEM]}

    listing = fetch_ls_embedded(client, access=FakeAccess(), src="remote:path/to")

    assert client.calls == [("operations/list", {"fs": "remote:", "remote": "path/to"})]
    assert len(listing.files) == 1
    assert len(listing.dirs) == 1


def test_fetch_ls_embedded_unlimited_recursion_sets_opt_recurse_only() -> None:
    client = FakeRcClient()
    client.responses["operations/list"] = {"list": []}

    fetch_ls_embedded(client, access=FakeAccess(), src="remote:path", max_depth=-1)

    assert client.calls == [
        ("operations/list", {"fs": "remote:", "remote": "path", "opt": {"recurse": True}})
    ]


def test_fetch_ls_embedded_bounded_recursion_sets_config_max_depth() -> None:
    client = FakeRcClient()
    client.responses["operations/list"] = {"list": []}

    fetch_ls_embedded(client, access=FakeAccess(), src="remote:path", max_depth=3)

    assert client.calls == [
        (
            "operations/list",
            {
                "fs": "remote:",
                "remote": "path",
                "opt": {"recurse": True},
                "_config": {"MaxDepth": 3},
            },
        )
    ]


def test_fetch_ls_embedded_zero_max_depth_is_non_recursive() -> None:
    client = FakeRcClient()
    client.responses["operations/list"] = {"list": []}

    fetch_ls_embedded(client, access=FakeAccess(), src="remote:path", max_depth=0)

    assert client.calls == [("operations/list", {"fs": "remote:", "remote": "path"})]


@pytest.mark.parametrize(
    ("listing_option", "expected_opt"),
    [
        (ListingOption.FILES_ONLY, {"filesOnly": True}),
        (ListingOption.DIRS_ONLY, {"dirsOnly": True}),
    ],
)
def test_fetch_ls_embedded_listing_option_maps_to_opt(
    listing_option: ListingOption, expected_opt: dict[str, bool]
) -> None:
    client = FakeRcClient()
    client.responses["operations/list"] = {"list": []}

    fetch_ls_embedded(client, access=FakeAccess(), src="remote:path", listing_option=listing_option)

    assert client.calls == [
        ("operations/list", {"fs": "remote:", "remote": "path", "opt": expected_opt})
    ]


def test_fetch_ls_embedded_applies_glob_filter() -> None:
    client = FakeRcClient()
    client.responses["operations/list"] = {
        "list": [
            _FILE_ITEM,
            {**_FILE_ITEM, "Path": "path/to/other.md", "Name": "other.md"},
        ]
    }

    listing = fetch_ls_embedded(client, access=FakeAccess(), src="remote:path/to", glob="*.txt")

    assert [f.name for f in listing.files] == ["object.txt"]


def test_fetch_ls_embedded_reverse_order() -> None:
    client = FakeRcClient()
    client.responses["operations/list"] = {
        "list": [
            _FILE_ITEM,
            {**_FILE_ITEM, "Path": "path/to/b.txt", "Name": "b.txt"},
        ]
    }

    listing = fetch_ls_embedded(
        client, access=FakeAccess(), src="remote:path/to", order=Order.REVERSE
    )

    assert [f.name for f in listing.files] == ["b.txt", "object.txt"]


def test_check_is_synced_embedded_returns_success_field() -> None:
    client = FakeRcClient()
    client.responses["operations/check"] = {"success": True, "status": "OK"}

    assert check_is_synced_embedded(client, "src:bucket", "dst:bucket") is True
    assert client.calls == [
        (
            "operations/check",
            {
                "srcFs": "src:bucket",
                "dstFs": "dst:bucket",
                "missingOnSrc": False,
                "missingOnDst": False,
                "match": False,
                "differ": False,
                "error": False,
            },
        )
    ]


def test_check_is_synced_embedded_false_when_not_success() -> None:
    client = FakeRcClient()
    client.responses["operations/check"] = {"success": False, "status": "1 differences found"}

    assert check_is_synced_embedded(client, "src:bucket", "dst:bucket") is False
