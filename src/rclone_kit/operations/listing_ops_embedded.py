"""Embedded RC-backed listing/stat operations (CLI-to-C-ABI migration ledger
rows M02, L05, L06/L07 (transitive), L08, L10).

Parallels `listing_ops.py`'s CLI-backed functions; `Rclone` dispatches to
whichever matches its `execution` mode. `operations/stat`'s `item` field is
non-null exactly when the target exists, which is a more direct and correct
check than the CLI backend's "does listing this path return any children"
approximation - the ledger notes this as an expected, sanctioned behavior
difference (L05, L10), not a regression to reconcile away.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rclone_kit.file import File
from rclone_kit.rc.client import RcCallable
from rclone_kit.rc.paths import RcPath
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath
from rclone_kit.types import SizeSuffix

if TYPE_CHECKING:
    from rclone_kit.access import ListingAccess


def _split_remote_name_and_path(src: str) -> tuple[str, str]:
    """Match `util.to_path()`'s existing colon-naive remote/path split, so a
    `File` built here models its `Remote` the same way every other
    rclone-kit value does - independent of `RcPath.parse`'s more careful
    Windows-drive-aware split, which is used only to address the RC call
    itself correctly.
    """
    parts = src.split(":")
    return parts[0], ":".join(parts[1:])


def _stat_item(rc_client: RcCallable, access: ListingAccess, src: str, *, files_only: bool) -> File:
    # `operations/stat`'s `fs` must be a navigable root, never a bare file
    # path, so the request always splits at the final path component -
    # unlike a listing call, which wants the whole remainder as `remote`.
    target = RcPath.parse(src).as_parent_and_name()
    params: dict[str, object] = {"fs": target.fs, "remote": target.remote}
    if files_only:
        params["opt"] = {"filesOnly": True}
    result = rc_client.call("operations/stat", **params)
    item = result.get("item")
    if item is None:
        raise FileNotFoundError(f"File not found: {src}")
    remote_name, path = _split_remote_name_and_path(src)
    remote = Remote(name=remote_name, rclone=access)
    rpath = RPath(
        remote=remote,
        path=path,
        name=item["Name"],
        size=item["Size"],
        mime_type=item["MimeType"],
        mod_time=item["ModTime"],
        is_dir=item["IsDir"],
    )
    rpath.set_rclone(access)
    return File(rpath)


def fetch_listremotes_embedded(rc_client: RcCallable, access: ListingAccess) -> list[Remote]:
    """List configured remotes via `config/listremotes`."""
    result = rc_client.call("config/listremotes")
    return [Remote(name=name, rclone=access) for name in result.get("remotes", [])]


def fetch_stat_embedded(rc_client: RcCallable, access: ListingAccess, src: str) -> File:
    """Get the status of a file or directory via `operations/stat`.

    Raises FileNotFoundError if `src` does not exist.
    """
    return _stat_item(rc_client, access, src, files_only=False)


def fetch_size_file_embedded(rc_client: RcCallable, access: ListingAccess, src: str) -> SizeSuffix:
    """Get the size of a file via `operations/stat` with `opt.filesOnly`.

    Raises FileNotFoundError if `src` does not name an existing file (a
    directory at `src`, or nothing at all).
    """
    file = _stat_item(rc_client, access, src, files_only=True)
    return SizeSuffix(file.size)


def check_exists_embedded(rc_client: RcCallable, access: ListingAccess, src: str) -> bool:
    """Check if a file or directory exists via `operations/stat`."""
    try:
        _stat_item(rc_client, access, src, files_only=False)
    except FileNotFoundError:
        return False
    return True
