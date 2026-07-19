"""Verified HTTPS downloader for immutable rclone release archives.

Downloading, hashing, and extracting are kept in separate modules
(`downloader.py`, `hashing.py`, `archive_extract.py`) so each concern stays
independently testable.
"""

import hashlib
from pathlib import Path

import httpx

from rclone_kit.runtime.exceptions import (
    ArchiveDigestMismatchError,
    ArchiveDownloadHttpError,
    ArchiveTruncatedDownloadError,
)
from rclone_kit.runtime.hashing import atomic_replace_file, best_effort_unlink, sha256_of_file
from rclone_kit.runtime.platform import RcloneArtifact

_DOWNLOAD_CHUNK_SIZE_BYTES = 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_PARTIAL_FILE_SUFFIX = ".part"
_CONTENT_LENGTH_HEADER = "content-length"


def fetch_verified_archive(
    artifact: RcloneArtifact,
    destination: Path,
    *,
    client: httpx.Client | None = None,
) -> Path:
    """Download and verify one immutable rclone release archive.

    Streams `artifact.download_url` to a temporary file beside `destination`
    and compares its SHA-256 digest against `artifact.sha256_digest` before
    replacing `destination`. `destination` is never left partially written or
    unverified: on any failure the temporary file is removed and
    `destination` is untouched.

    `client` may be supplied for testing with a fake `httpx.Transport`; a
    default `httpx.Client` is created and closed otherwise.

    Raises `ArchiveDownloadHttpError` on a non-2xx response or transport
    failure, `ArchiveTruncatedDownloadError` when fewer bytes arrive than the
    advertised `Content-Length`, and `ArchiveDigestMismatchError` when the
    computed digest does not match `artifact.sha256_digest`.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(destination.name + _PARTIAL_FILE_SUFFIX)
    owns_client = client is None
    active_client = client if client is not None else httpx.Client()
    try:
        _stream_download_to_file(artifact, temp_path, active_client)
        _verify_downloaded_digest(artifact, temp_path)
        atomic_replace_file(temp_path, destination)
    finally:
        best_effort_unlink(temp_path)
        if owns_client:
            active_client.close()
    return destination


def _stream_download_to_file(
    artifact: RcloneArtifact, temp_path: Path, client: httpx.Client
) -> None:
    try:
        with client.stream(
            "GET", artifact.download_url, timeout=_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
        ) as response:
            response.raise_for_status()
            expected_length = _parse_content_length(response.headers.get(_CONTENT_LENGTH_HEADER))
            bytes_written = _write_response_body(response, temp_path)
    except httpx.HTTPStatusError as error:
        raise ArchiveDownloadHttpError(artifact.download_url, error.response.status_code) from error
    except httpx.HTTPError as error:
        raise ArchiveDownloadHttpError(artifact.download_url, None) from error
    _raise_if_truncated(artifact, expected_length, bytes_written)


def _write_response_body(response: httpx.Response, temp_path: Path) -> int:
    bytes_written = 0
    with temp_path.open("wb") as handle:
        for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_SIZE_BYTES):
            handle.write(chunk)
            bytes_written += len(chunk)
    return bytes_written


def _parse_content_length(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None
    return int(raw_value)


def _raise_if_truncated(
    artifact: RcloneArtifact, expected_length: int | None, actual_length: int
) -> None:
    if expected_length is not None and actual_length != expected_length:
        raise ArchiveTruncatedDownloadError(artifact.download_url, expected_length, actual_length)


def _verify_downloaded_digest(artifact: RcloneArtifact, temp_path: Path) -> None:
    actual_digest = sha256_of_file(temp_path)
    if actual_digest != artifact.sha256_digest:
        raise ArchiveDigestMismatchError(
            artifact.download_url, artifact.sha256_digest, actual_digest
        )


def sha256_hexdigest(data: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of in-memory `data`.

    Exposed for tests that build small byte fixtures instead of files.
    """
    return hashlib.sha256(data).hexdigest()
