from __future__ import annotations

import logging
from datetime import datetime

from rclone_kit.access import ListingAccess
from rclone_kit.file import File
from rclone_kit.types import SizeResult

logger = logging.getLogger(__name__)

_MIN_FILES_FOR_BATCH_LISTING = 2


def print_contents(access: ListingAccess, src: str) -> None:
    """Print the contents of a file."""
    print(access.read_text(src))  # noqa: T201 - stdout is the entire point of Rclone.print()


def fetch_modtime(access: ListingAccess, src: str) -> str:
    """Get the modification time of a file or directory."""
    return access.stat(src).mod_time()


def fetch_modtime_dt(access: ListingAccess, src: str) -> datetime:
    """Get the modification time of a file or directory."""
    return access.stat(src).mod_time_dt()


def build_size_result(src: str, all_files: list[File]) -> SizeResult:
    """Fold a flat listing of `File`s under `src` into a `SizeResult`,
    deduplicating and logging a warning for any zero-size or duplicate
    entry, and raising on an out-of-tree one.

    Used by `listing_ops_embedded.fetch_size_files_embedded`.
    """
    file_sizes: dict[str, int] = {}
    f: File
    for f in all_files:
        p = f.to_string(include_remote=True)
        if p in file_sizes:
            logger.warning("Duplicate file found: %s", p)
            continue
        size = f.size
        if size == 0:
            logger.warning("File size is 0: %s", p)
        file_sizes[p] = f.size
    total_size = sum(file_sizes.values())
    file_sizes_path_corrected: dict[str, int] = {}
    for path, size in file_sizes.items():
        prefix = src.rstrip("/") + "/"
        if not path.startswith(prefix):
            raise ValueError(f"Listed path {path!r} is outside source {src!r}")
        file_sizes_path_corrected[path.removeprefix(prefix)] = size
    return SizeResult(prefix=src, total_size=total_size, file_sizes=file_sizes_path_corrected)
