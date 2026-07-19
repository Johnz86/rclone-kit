"""Shared safe zip-extraction primitives.

Both the runtime verified-download fallback and the build-time artifact
preparation script (`scripts/prepare_rclone_artifact.py`) extract exactly one
named executable from an otherwise untrusted zip archive. This module holds
that logic once so neither caller re-implements archive traversal safety.
"""

import shutil
import zipfile
from pathlib import Path, PurePosixPath

from rclone_kit.runtime.exceptions import (
    ArchiveMemberDuplicateError,
    ArchiveMemberMissingError,
    ArchiveMemberUnsafeError,
)


def extract_single_member(archive_path: Path, member_name: str, destination: Path) -> Path:
    """Extract exactly one named member from a zip archive to `destination`.

    Looks up `member_name` by exact match against each entry's recorded
    filename rather than delegating to `ZipFile.extract`, so an unsafe or
    duplicated entry is rejected before any bytes are written.

    Raises `ArchiveMemberMissingError` when `member_name` is absent,
    `ArchiveMemberDuplicateError` when it appears more than once, and
    `ArchiveMemberUnsafeError` when its recorded path is absolute or escapes
    the archive root through a parent-directory segment.
    """
    with zipfile.ZipFile(archive_path) as archive:
        member = _find_member(archive, member_name)
        _reject_unsafe_member_path(member.filename)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)
    return destination


def _find_member(archive: zipfile.ZipFile, member_name: str) -> zipfile.ZipInfo:
    matches = [info for info in archive.infolist() if info.filename == member_name]
    if not matches:
        raise ArchiveMemberMissingError(member_name)
    if len(matches) > 1:
        raise ArchiveMemberDuplicateError(member_name)
    return matches[0]


def _reject_unsafe_member_path(member_filename: str) -> None:
    pure_path = PurePosixPath(member_filename)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        raise ArchiveMemberUnsafeError(member_filename)
