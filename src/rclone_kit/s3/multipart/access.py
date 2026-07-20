"""Structural access contract for multipart upload orchestration."""

from __future__ import annotations

from typing import Protocol

from rclone_kit.completed_process import CompletedProcess
from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.file import File
from rclone_kit.http_server import HttpServer
from rclone_kit.remote import Remote
from rclone_kit.s3.types import S3Credentials
from rclone_kit.types import ListingOption, Order, SizeSuffix


class MultipartAccess(Protocol):
    """High-level callbacks shared by multipart strategies."""

    def ls(
        self,
        src: Dir | Remote | str | None = None,
        max_depth: int | None = None,
        glob: str | None = None,
        order: Order = Order.NORMAL,
        listing_option: ListingOption = ListingOption.ALL,
    ) -> DirListing: ...

    def read_text(self, src: str) -> str: ...

    def stat(self, src: str) -> File: ...

    def write_text(self, text: str, dst: str) -> None: ...

    def print(self, src: str) -> None: ...

    def copy_to(self, src: str, dst: str) -> CompletedProcess: ...

    def size_file(self, src: str) -> SizeSuffix: ...

    def serve_http(
        self,
        src: str,
        addr: str | None = None,
        other_args: list[str] | None = None,
    ) -> HttpServer: ...

    def exists(self, src: str) -> bool: ...

    def purge(self, src: str) -> CompletedProcess: ...

    def get_s3_credentials(
        self,
        remote: str,
        verbose: bool | None = None,
    ) -> S3Credentials: ...
