"""Embedded RC-backed listing/stat operations.

`operations/stat`'s `item` field is non-null exactly when the target
exists, which is a more direct and correct check than "does listing this
path return any children" would be.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Generator
from fnmatch import fnmatch
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

from rclone_kit.convert import convert_to_str
from rclone_kit.diff import DiffItem, DiffOption, DiffType
from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.embedded_file_stream import EmbeddedFilesStream
from rclone_kit.file import File
from rclone_kit.operations.listing_ops import _MIN_FILES_FOR_BATCH_LISTING, build_size_result
from rclone_kit.rc.client import RcCallable
from rclone_kit.rc.errors import RcCallError
from rclone_kit.rc.paths import RcPath, split_remote_and_path
from rclone_kit.remote import Remote
from rclone_kit.rpath import RcloneJsonEntry, RPath
from rclone_kit.types import ListingOption, Order, SizeResult, SizeSuffix
from rclone_kit.util import get_check, to_path, write_files_from

if TYPE_CHECKING:
    from rclone_kit.access import ListingAccess
    from rclone_kit.rc.list_stream import RcListStreamClient

logger = logging.getLogger(__name__)

_DIFF_OPTION_TO_RC_FLAG = {
    DiffOption.COMBINED: "combined",
    DiffOption.MISSING_ON_SRC: "missingOnSrc",
    DiffOption.MISSING_ON_DST: "missingOnDst",
    DiffOption.DIFFER: "differ",
    DiffOption.MATCH: "match",
    DiffOption.ERROR: "error",
}

_DIFF_OPTION_TO_DIFF_TYPE = {
    DiffOption.MISSING_ON_SRC: DiffType.MISSING_ON_SRC,
    DiffOption.MISSING_ON_DST: DiffType.MISSING_ON_DST,
    DiffOption.DIFFER: DiffType.DIFFERENT,
    DiffOption.MATCH: DiffType.EQUAL,
    DiffOption.ERROR: DiffType.ERROR,
}


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
    remote_name, path = split_remote_and_path(src)
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

    `src=None` lists configured remotes as root directories (no RC listing
    call involved); `max_depth<0` recurses without limit, `max_depth>0`
    recurses bounded by `_config.MaxDepth`, and `None`/`0` list only the
    immediate target - matching rclone's own `--recursive`/`--max-depth`
    semantics.
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

    # `str(src)` reconstructs the exact original combined path (`RPath.
    # __str__` always yields the original string back, whatever the
    # `Remote.name`/`RPath.path` split - see `rc.paths.split_remote_and_path`),
    # which `RcPath.parse` then splits Windows-drive-aware for the RC call.
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


def fetch_size_files_embedded(
    rc_client: RcCallable,
    access: ListingAccess,
    src: str,
    files: list[str],
    fast_list: bool = False,
    check: bool | None = False,
) -> SizeResult:
    """Get the size of a list of files via one `operations/list` RC call:
    unlike the partitioned `copy_files_embedded`/`delete_files_embedded`,
    this never partitions - a single call already covers every requested
    file regardless of how many directories/remotes they span.

    `check=True` (never the default) lets the RC failure (`RcCallError`)
    propagate; `check=False` logs a warning and reports an empty listing
    instead.
    """
    check = get_check(check)
    if not files:
        return SizeResult(prefix=src, total_size=0, file_sizes={})
    if len(files) < _MIN_FILES_FOR_BATCH_LISTING:
        full_path = f"{src}/{files[0]}"
        tmp = access.size_file(full_path)
        return SizeResult(prefix=src, total_size=tmp.as_int(), file_sizes={files[0]: tmp.as_int()})
    if fast_list:
        logger.warning(
            "It's not recommended to use --fast-list with size_files as this will perform "
            "poorly on large repositories since the entire repository has to be scanned."
        )

    target = RcPath.parse(src)
    params: dict[str, object] = {
        "fs": target.fs,
        "remote": target.remote,
        "opt": {"filesOnly": True, "recurse": True},
    }
    if fast_list:
        params["_config"] = {"UseListR": True}

    with TemporaryDirectory() as tmpdir:
        include_files_txt = write_files_from(tmpdir, list(files))
        params["_filter"] = {"FilesFrom": [str(include_files_txt)]}
        try:
            result = rc_client.call("operations/list", **params)
        except RcCallError as error:
            if check:
                raise
            logger.warning("Error getting file sizes: %s", error)
            result = {"list": []}

    remote_name, parent_path = split_remote_and_path(src)
    remote = Remote(name=remote_name, rclone=access)
    all_files = [
        File(RPath.from_dict(cast(RcloneJsonEntry, item), remote, parent_path=parent_path))
        for item in result.get("list", [])
    ]
    return build_size_result(src, all_files)


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
    this method doesn't use) and returns it directly. An unexpected RC
    failure (bad path, missing backend, ...) raises `RcCallError` instead
    of silently returning `False`.
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


_COMBINED_PREFIX_TO_DIFF_TYPE = {diff_type.value: diff_type for diff_type in DiffType}


def _classify_combined_line(line: str) -> tuple[DiffType, str] | None:
    if not line:
        return None
    diff_type = _COMBINED_PREFIX_TO_DIFF_TYPE.get(line[0])
    if diff_type is None:
        return None
    return diff_type, line[1:].strip()


def stream_diff_embedded(
    rc_client: RcCallable,
    src: str,
    dst: str,
    min_size: str | None = None,
    max_size: str | None = None,
    diff_option: DiffOption = DiffOption.COMBINED,
    fast_list: bool = True,
    size_only: bool | None = None,
    checkers: int | None = None,
) -> Generator[DiffItem]:
    """Compare `src` and `dst` via one `operations/check` RC call, requesting
    only the report array `diff_option` needs.

    Every `DiffOption` value is supported: `operations/check` already
    returns `differ`/`match`/`error` arrays directly, at no extra request
    cost.
    """
    if size_only is None:
        size_only = diff_option in (DiffOption.MISSING_ON_DST, DiffOption.MISSING_ON_SRC)

    report_flag = _DIFF_OPTION_TO_RC_FLAG[diff_option]
    params: dict[str, object] = {
        "srcFs": src,
        "dstFs": dst,
        "combined": False,
        "missingOnSrc": False,
        "missingOnDst": False,
        "match": False,
        "differ": False,
        "error": False,
    }
    params[report_flag] = True
    if diff_option == DiffOption.MISSING_ON_DST:
        params["oneWay"] = True

    config: dict[str, object] = {}
    if size_only:
        config["SizeOnly"] = True
    if checkers is not None and checkers >= 1:
        config["Checkers"] = checkers
    if fast_list:
        config["UseListR"] = True
    if config:
        params["_config"] = config

    filter_opt: dict[str, object] = {}
    if min_size:
        filter_opt["MinSize"] = min_size
    if max_size:
        filter_opt["MaxSize"] = max_size
    if filter_opt:
        params["_filter"] = filter_opt

    result = rc_client.call("operations/check", **params)

    if diff_option == DiffOption.COMBINED:
        for line in result.get("combined", []):
            classified = _classify_combined_line(line)
            if classified is None:
                continue
            diff_type, path = classified
            yield DiffItem(diff_type, path, src_prefix=src, dst_prefix=dst)
        return

    diff_type = _DIFF_OPTION_TO_DIFF_TYPE[diff_option]
    for path in result.get(report_flag, []):
        yield DiffItem(diff_type, path, src_prefix=src, dst_prefix=dst)


def fetch_ls_stream_embedded(
    list_stream_client: RcListStreamClient,
    src: str,
    max_depth: int = -1,
    fast_list: bool = False,
) -> EmbeddedFilesStream:
    """Open a bounded-memory listing stream via `rclonekit/liststream/open`.

    `max_depth < 0` or `> 1` recurses (bounded via `_config.MaxDepth` when
    `> 1`), matching `fetch_ls_embedded`'s own `max_depth` mapping;
    `fast_list` sets `_config.UseListR`.
    """
    target = RcPath.parse(src)
    opt: dict[str, object] = {"filesOnly": True}
    config: dict[str, object] = {}
    recurse = max_depth < 0 or max_depth > 1
    if recurse:
        opt["recurse"] = True
        if max_depth > 1:
            config["MaxDepth"] = max_depth
    if fast_list:
        config["UseListR"] = True

    stream_id = list_stream_client.open(target.fs, target.remote, opt=opt, config=config)
    return EmbeddedFilesStream(list_stream_client, src, stream_id)
