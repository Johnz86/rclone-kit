"""Unit tests for `scripts/prepare_rclone_artifact.py`.

`scripts` is on `sys.path` via `[tool.pytest.ini_options] pythonpath`, so the
script is importable as a plain top-level module.

`stage_executable` composes `fetch_verified_archive` and
`extract_single_member`; both are monkeypatched here to a fake extraction
that writes controlled bytes, so these tests exercise the digest-mismatch
guard without a real network download.
"""

import dataclasses
import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

import prepare_rclone_artifact
from rclone_kit.runtime.exceptions import StagedExecutableDigestMismatchError
from rclone_kit.runtime.platform import MachineArchitecture, OperatingSystem, RcloneArtifact

_VALID_EXECUTABLE_CONTENT = b"genuine-rclone-executable-bytes"
_VALID_EXECUTABLE_DIGEST = hashlib.sha256(_VALID_EXECUTABLE_CONTENT).hexdigest()
_CORRUPTED_EXECUTABLE_CONTENT = b"corrupted-during-extraction"

_TEST_ARTIFACT = RcloneArtifact(
    operating_system=OperatingSystem.LINUX,
    architecture=MachineArchitecture.AMD64,
    archive_filename="rclone-test-linux-amd64.zip",
    download_url="https://example.invalid/rclone-test-linux-amd64.zip",
    sha256_digest="1" * 64,
    executable_member_name="rclone-test/rclone",
    executable_name="rclone",
    executable_sha256_digest=_VALID_EXECUTABLE_DIGEST,
    wheel_platform_tag="manylinux2014_x86_64",
)


def _fake_fetch_verified_archive(_artifact: RcloneArtifact, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"fake-archive-bytes")
    return destination


def _fake_extract_matching_content(content: bytes) -> Callable[[Path, str, Path], Path]:
    def _extract(_archive_path: Path, _member_name: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    return _extract


@pytest.fixture
def _patched_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        prepare_rclone_artifact, "fetch_verified_archive", _fake_fetch_verified_archive
    )


@pytest.mark.usefixtures("_patched_download")
def test_stage_executable_writes_verified_executable_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        prepare_rclone_artifact,
        "extract_single_member",
        _fake_extract_matching_content(_VALID_EXECUTABLE_CONTENT),
    )
    staging_dir = tmp_path / "staged"
    staging_dir.mkdir()

    executable_path = prepare_rclone_artifact.stage_executable(
        _TEST_ARTIFACT, staging_dir, archive_cache_dir=tmp_path / "archive-cache"
    )

    assert executable_path.read_bytes() == _VALID_EXECUTABLE_CONTENT
    manifest_path = executable_path.with_name(executable_path.name + ".sha256")
    assert manifest_path.read_text(encoding="utf-8") == _VALID_EXECUTABLE_DIGEST


@pytest.mark.usefixtures("_patched_download")
def test_stage_executable_raises_on_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        prepare_rclone_artifact,
        "extract_single_member",
        _fake_extract_matching_content(_CORRUPTED_EXECUTABLE_CONTENT),
    )
    staging_dir = tmp_path / "staged"
    staging_dir.mkdir()

    with pytest.raises(StagedExecutableDigestMismatchError):
        prepare_rclone_artifact.stage_executable(
            _TEST_ARTIFACT, staging_dir, archive_cache_dir=tmp_path / "archive-cache"
        )


@pytest.mark.usefixtures("_patched_download")
def test_stage_executable_does_not_leave_corrupted_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        prepare_rclone_artifact,
        "extract_single_member",
        _fake_extract_matching_content(_CORRUPTED_EXECUTABLE_CONTENT),
    )
    staging_dir = tmp_path / "staged"
    staging_dir.mkdir()

    with pytest.raises(StagedExecutableDigestMismatchError):
        prepare_rclone_artifact.stage_executable(
            _TEST_ARTIFACT, staging_dir, archive_cache_dir=tmp_path / "archive-cache"
        )

    assert list(staging_dir.iterdir()) == []


def test_cached_verified_archive_reuses_valid_cache_entry_without_redownloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached_bytes = b"already-cached-and-verified-archive-bytes"
    artifact = dataclasses.replace(
        _TEST_ARTIFACT, sha256_digest=hashlib.sha256(cached_bytes).hexdigest()
    )
    cache_root = tmp_path / "archive-cache"
    cache_path = (
        cache_root
        / prepare_rclone_artifact.RCLONE_VERSION
        / artifact.wheel_platform_tag
        / artifact.archive_filename
    )
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(cached_bytes)

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("fetch_verified_archive must not run on a cache hit")

    monkeypatch.setattr(prepare_rclone_artifact, "fetch_verified_archive", fail_if_called)

    result = prepare_rclone_artifact._cached_verified_archive(artifact, cache_root)

    assert result == cache_path


def test_cached_verified_archive_redownloads_when_cache_entry_is_corrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_root = tmp_path / "archive-cache"
    cache_path = (
        cache_root
        / prepare_rclone_artifact.RCLONE_VERSION
        / _TEST_ARTIFACT.wheel_platform_tag
        / _TEST_ARTIFACT.archive_filename
    )
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"corrupted-cache-entry-does-not-match-digest")
    calls: list[Path] = []

    def fake_fetch(_artifact: RcloneArtifact, destination: Path) -> Path:
        calls.append(destination)
        destination.write_bytes(b"freshly-downloaded-bytes")
        return destination

    monkeypatch.setattr(prepare_rclone_artifact, "fetch_verified_archive", fake_fetch)

    result = prepare_rclone_artifact._cached_verified_archive(_TEST_ARTIFACT, cache_root)

    assert calls == [cache_path]
    assert result.read_bytes() == b"freshly-downloaded-bytes"


def test_stage_license_copies_vendored_license_text(tmp_path: Path) -> None:
    staging_dir = tmp_path / "staged"
    staging_dir.mkdir()

    license_path = prepare_rclone_artifact.stage_license(staging_dir)

    assert license_path.read_text(encoding="utf-8") == (
        prepare_rclone_artifact._RCLONE_LICENSE_SOURCE.read_text(encoding="utf-8")
    )


def test_staging_directory_nests_by_wheel_platform_tag(tmp_path: Path) -> None:
    result = prepare_rclone_artifact.staging_directory(tmp_path, _TEST_ARTIFACT)

    assert result == tmp_path / "rclone" / _TEST_ARTIFACT.wheel_platform_tag
