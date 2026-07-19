"""Unit tests for `scripts/prepare_rclone_artifact.py`.

`scripts` is on `sys.path` via `[tool.pytest.ini_options] pythonpath`, so the
script is importable as a plain top-level module.

`stage_executable` composes `fetch_verified_archive` and
`extract_single_member`; both are monkeypatched here to a fake extraction
that writes controlled bytes, so these tests exercise the digest-mismatch
guard without a real network download.
"""

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

    executable_path = prepare_rclone_artifact.stage_executable(_TEST_ARTIFACT, staging_dir)

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
        prepare_rclone_artifact.stage_executable(_TEST_ARTIFACT, staging_dir)


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
        prepare_rclone_artifact.stage_executable(_TEST_ARTIFACT, staging_dir)

    assert list(staging_dir.iterdir()) == []


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
