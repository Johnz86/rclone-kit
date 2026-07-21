from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from rclone_kit.file_part import FilePart
from rclone_kit.s3.multipart.file_info import S3FileInfo


class S3Provider(Enum):
    S3 = "s3"
    BACKBLAZE = "b2"
    DIGITAL_OCEAN = "DigitalOcean"

    @staticmethod
    def from_str(value: str) -> "S3Provider":
        """Map an rclone `provider` config string to a `S3Provider`.

        Only Backblaze B2 needs a distinct S3 client (unsigned payloads, a
        default endpoint) - see `create_s3_client`. Every other provider
        string - AWS, DigitalOcean, Ceph, MinIO, Wasabi, or any other
        S3-compatible endpoint rclone supports - uses the same generic
        client, so anything not recognized as Backblaze or DigitalOcean
        falls back to the generic `S3` provider instead of raising.
        """
        if value == "b2":
            return S3Provider.BACKBLAZE
        if value == "DigitalOcean":
            return S3Provider.DIGITAL_OCEAN
        return S3Provider.S3


@dataclass
class S3Credentials:
    """Credentials for accessing S3."""

    bucket_name: str
    provider: S3Provider
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None
    region_name: str | None = None
    endpoint_url: str | None = None


@dataclass
class S3UploadTarget:
    """Target information for S3 upload."""

    src_file: Path
    src_file_size: int | None
    bucket_name: str
    s3_key: str


@dataclass
class S3MutliPartUploadConfig:
    """Input for multi-part upload."""

    chunk_size: int
    retries: int
    chunk_fetcher: Callable[[int, int, S3FileInfo], Future[FilePart]]
    resume_path_json: Path
    max_write_threads: int
    max_chunks_before_suspension: int | None = None
    mount_path: Path | None = None


class MultiUploadResult(Enum):
    UPLOADED_FRESH = 1
    UPLOADED_RESUME = 2
    SUSPENDED = 3
    ALREADY_DONE = 4
