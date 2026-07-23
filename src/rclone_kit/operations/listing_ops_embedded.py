"""Embedded RC-backed listing/stat operations (CLI-to-C-ABI migration ledger
rows L01, M02, L05, L06/L07 (transitive), L08, L10, L11).

Parallels `listing_ops.py`'s CLI-backed functions; `Rclone` dispatches to
whichever matches its `execution` mode. `operations/stat`'s `item` field is
non-null exactly when the target exists, which is a more direct and correct
check than the CLI backend's "does listing this path return any children"
approximation - the ledger notes this as an expected, sanctioned behavior
difference (L05, L10), not a regression to reconcile away.
"""

from __future__ import annotations

import random
from fnmatch import fnmatch
from typing import TYPE_CHECKING, cast

from rclone_kit.convert import convert_to_str
from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.file import File
from rclone_kit.rc.client import RcCallable
from rclone_kit.rc.paths import RcPath
from rclone_kit.remote import Remote
from rclone_kit.rpath import RcloneJsonEntry, RPath
from rclone_kit.types import ListingOption, Order, SizeSuffix
from rclone_kit.util import to_path

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


def fetch_ls_embedded(
    rc_client: RcCallable,
    access: ListingAccess,
    src: Dir | Remote | str | None = None,
    max_depth: int | None = None,
    glob: str | None = None,
    order: Order = Order.NORMAL,
    listing_option: ListingOption = ListingOption.ALL,
) -> DirListing:
    """List files in the given path via `operations/list`.

    Mirrors `listing_ops.fetch_ls`'s exact semantics: `src=None` lists
    configured remotes as root directories (no RC listing call involved,
    same as the CLI path); `max_depth<0` recurses without limit,
    `max_depth>0` recurses bounded by `_config.MaxDepth`, and `None`/`0`
    list only the immediate target - matching `--recursive`/`--max-depth`'s
    CLI behavior exactly.
    """
    if src is None:
        list_remotes = fetch_listremotes_embedded(rc_client, access)
        dirs = [Dir(remote) for remote in list_remotes]
        for d in dirs:
            d.path.path = ""
        return DirListing([d.path for d in dirs])

    if isinstance(src, str):
        src = Dir(to_path(src, access))

    remote = src.remote if isinstance(src, Dir) else src
    parent_path = src.path.path if isinstance(src, Dir) else None

    # `str(src)` reconstructs the exact original combined path (a `Dir`'s
    # `Remote.name`/`RPath.path` split can itself be colon-naive for local
    # paths - see `_split_remote_name_and_path` - but concatenating them
    # back via `str()` always yields the original string), which
    # `RcPath.parse` then splits Windows-drive-aware for the RC call.
    target = RcPath.parse(str(src))
    opt: dict[str, object] = {}
    if listing_option == ListingOption.FILES_ONLY:
        opt["filesOnly"] = True
    elif listing_option == ListingOption.DIRS_ONLY:
        opt["dirsOnly"] = True
    config: dict[str, object] = {}
    if max_depth is not None and max_depth != 0:
        opt["recurse"] = True
        if max_depth > 0:
            config["MaxDepth"] = max_depth

    params: dict[str, object] = {"fs": target.fs, "remote": target.remote}
    if opt:
        params["opt"] = opt
    if config:
        params["_config"] = config

    result = rc_client.call("operations/list", **params)
    paths = [
        RPath.from_dict(cast(RcloneJsonEntry, item), remote, parent_path=parent_path)
        for item in result.get("list", [])
    ]
    for p in paths:
        p.set_rclone(access)

    if glob is not None:
        paths = [p for p in paths if fnmatch(p.path, glob)]
    if order == Order.REVERSE:
        paths.reverse()
    elif order == Order.RANDOM:
        random.shuffle(paths)
    return DirListing(paths)


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


def check_is_synced_embedded(rc_client: RcCallable, src: str | Dir, dst: str | Dir) -> bool:
    """Check if two directories are in sync via `operations/check`.

    Requests only the `success` field's inputs (no per-file reports, which
    this method doesn't use) and returns it directly. Unlike the CLI
    backend, which conflates every nonzero return code with "not synced",
    an unexpected RC failure (bad path, missing backend, ...) raises
    `RcCallError` instead of silently returning `False`.
    """
    result = rc_client.call(
        "operations/check",
        srcFs=convert_to_str(src),
        dstFs=convert_to_str(dst),
        missingOnSrc=False,
        missingOnDst=False,
        match=False,
        differ=False,
        error=False,
    )
    return bool(result.get("success", False))
