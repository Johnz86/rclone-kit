"""Small structural contracts used by client-bound domain operations."""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from rclone_kit.dir import Dir
    from rclone_kit.dir_listing import DirListing
    from rclone_kit.file import File
    from rclone_kit.remote import Remote
    from rclone_kit.types import ListingOption, Order, SizeSuffix


class DomainAccess(Protocol):
    """Capabilities retained by client-bound ``Dir`` and ``File`` values."""

    def _run(
        self,
        cmd: list[str],
        check: bool = False,
        capture: bool | Path | None = None,
    ) -> subprocess.CompletedProcess[str]: ...

    def ls(
        self,
        src: Dir | Remote | str | None = None,
        max_depth: int | None = None,
        glob: str | None = None,
        order: Order = ...,
        listing_option: ListingOption = ...,
    ) -> DirListing: ...

    def walk(
        self,
        src: Dir | Remote | str,
        max_depth: int = -1,
        breadth_first: bool = True,
        order: Order = ...,
    ) -> Generator[DirListing]: ...


class ListingAccess(DomainAccess, Protocol):
    """High-level callbacks required by listing orchestration."""

    def listremotes(self) -> list[Remote]: ...

    def read_text(self, src: str) -> str: ...

    def stat(self, src: str) -> File: ...

    def size_file(self, src: str) -> SizeSuffix: ...
