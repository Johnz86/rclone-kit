"""Unit tests for `rclone_kit.runtime.downloader`.

These tests use `httpx.MockTransport` so no real socket is ever opened; the
archive fixture is a small in-memory zip, not a real rclone release.
"""

import hashlib
import io
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from rclone_kit.runtime.downloader import fetch_verified_archive
from rclone_kit.runtime.exceptions import (
    ArchiveDigestMismatchError,
    ArchiveDownloadHttpError,
    ArchiveTruncatedDownloadError,
)
from rclone_kit.runtime.platform import MachineArchitecture, OperatingSystem, RcloneArtifact

_MEMBER_NAME = "rclone-test/rclone"
_MEMBER_CONTENT = b"fake-rclone-binary-bytes"


def _build_zip_bytes(member_name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member_name, content)
    return buffer.getvalue()


_ARCHIVE_BYTES = _build_zip_bytes(_MEMBER_NAME, _MEMBER_CONTENT)
_ARCHIVE_DIGEST = hashlib.sha256(_ARCHIVE_BYTES).hexdigest()

_TEST_ARTIFACT = RcloneArtifact(
    operating_system=OperatingSystem.LINUX,
    architecture=MachineArchitecture.AMD64,
    archive_filename="rclone-test-linux-amd64.zip",
    download_url="https://example.invalid/rclone-test-linux-amd64.zip",
    sha256_digest=_ARCHIVE_DIGEST,
    executable_member_name=_MEMBER_NAME,
    executable_name="rclone",
    executable_sha256_digest=hashlib.sha256(_MEMBER_CONTENT).hexdigest(),
    wheel_platform_tag="manylinux2014_x86_64",
)


def _client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _partial_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".part")


def test_fetch_verified_archive_succeeds_on_matching_digest(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_ARCHIVE_BYTES, headers={"content-length": str(len(_ARCHIVE_BYTES))}
        )

    destination = tmp_path / "archive.zip"

    result = fetch_verified_archive(_TEST_ARTIFACT, destination, client=_client_for(handler))

    assert result == destination
    assert destination.read_bytes() == _ARCHIVE_BYTES
    assert not _partial_path(destination).exists()


def test_fetch_verified_archive_raises_on_digest_mismatch(tmp_path: Path) -> None:
    mismatched_artifact = replace(_TEST_ARTIFACT, sha256_digest="0" * 64)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_ARCHIVE_BYTES, headers={"content-length": str(len(_ARCHIVE_BYTES))}
        )

    destination = tmp_path / "archive.zip"

    with pytest.raises(ArchiveDigestMismatchError):
        fetch_verified_archive(mismatched_artifact, destination, client=_client_for(handler))

    assert not destination.exists()
    assert not _partial_path(destination).exists()


def test_fetch_verified_archive_raises_on_truncated_download(tmp_path: Path) -> None:
    advertised_length = len(_ARCHIVE_BYTES) + 16

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_ARCHIVE_BYTES, headers={"content-length": str(advertised_length)}
        )

    destination = tmp_path / "archive.zip"

    with pytest.raises(ArchiveTruncatedDownloadError) as excinfo:
        fetch_verified_archive(_TEST_ARTIFACT, destination, client=_client_for(handler))

    assert excinfo.value.expected_bytes == advertised_length
    assert excinfo.value.actual_bytes == len(_ARCHIVE_BYTES)
    assert not destination.exists()
    assert not _partial_path(destination).exists()


def test_fetch_verified_archive_raises_on_http_error_status(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    destination = tmp_path / "archive.zip"

    with pytest.raises(ArchiveDownloadHttpError) as excinfo:
        fetch_verified_archive(_TEST_ARTIFACT, destination, client=_client_for(handler))

    assert excinfo.value.status_code == 404
    assert not destination.exists()
    assert not _partial_path(destination).exists()


def test_fetch_verified_archive_raises_on_transport_error(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure")

    destination = tmp_path / "archive.zip"

    with pytest.raises(ArchiveDownloadHttpError) as excinfo:
        fetch_verified_archive(_TEST_ARTIFACT, destination, client=_client_for(handler))

    assert excinfo.value.status_code is None
