"""Unit tests for `rclone_kit.runtime.platform`."""

from dataclasses import dataclass

import pytest

from rclone_kit.runtime.exceptions import UnsupportedPlatformError
from rclone_kit.runtime.platform import (
    LINUX_AMD64_ARTIFACT,
    WINDOWS_AMD64_ARTIFACT,
    RcloneArtifact,
    resolve_rclone_artifact,
)


@dataclass(frozen=True)
class PlatformMappingCase:
    system: str
    machine: str
    expected_artifact: RcloneArtifact


WINDOWS_AMD64_EXACT_CASE = PlatformMappingCase("Windows", "AMD64", WINDOWS_AMD64_ARTIFACT)
WINDOWS_AMD64_LOWERCASE_CASE = PlatformMappingCase("windows", "amd64", WINDOWS_AMD64_ARTIFACT)
LINUX_AMD64_CASE = PlatformMappingCase("Linux", "AMD64", LINUX_AMD64_ARTIFACT)
LINUX_X86_64_CASE = PlatformMappingCase("Linux", "x86_64", LINUX_AMD64_ARTIFACT)

PLATFORM_MAPPING_CASES = [
    WINDOWS_AMD64_EXACT_CASE,
    WINDOWS_AMD64_LOWERCASE_CASE,
    LINUX_AMD64_CASE,
    LINUX_X86_64_CASE,
]
PLATFORM_MAPPING_IDS = [
    "windows_amd64_exact",
    "windows_amd64_lowercase",
    "linux_amd64",
    "linux_x86_64",
]


@pytest.mark.parametrize("case", PLATFORM_MAPPING_CASES, ids=PLATFORM_MAPPING_IDS)
def test_resolve_rclone_artifact_maps_known_platforms(case: PlatformMappingCase) -> None:
    artifact = resolve_rclone_artifact(system=case.system, machine=case.machine)
    assert artifact == case.expected_artifact


def test_resolve_rclone_artifact_rejects_unknown_system() -> None:
    with pytest.raises(UnsupportedPlatformError) as excinfo:
        resolve_rclone_artifact(system="Darwin", machine="arm64")
    assert excinfo.value.system == "Darwin"
    assert excinfo.value.machine == "arm64"


def test_resolve_rclone_artifact_rejects_unknown_machine() -> None:
    with pytest.raises(UnsupportedPlatformError):
        resolve_rclone_artifact(system="Linux", machine="aarch64")


def test_artifacts_have_distinct_wheel_platform_tags() -> None:
    assert WINDOWS_AMD64_ARTIFACT.wheel_platform_tag != LINUX_AMD64_ARTIFACT.wheel_platform_tag


def test_artifact_sha256_digests_are_64_character_hex() -> None:
    for artifact in (WINDOWS_AMD64_ARTIFACT, LINUX_AMD64_ARTIFACT):
        assert len(artifact.sha256_digest) == 64
        int(artifact.sha256_digest, 16)


def test_artifact_executable_sha256_digests_are_64_character_hex() -> None:
    for artifact in (WINDOWS_AMD64_ARTIFACT, LINUX_AMD64_ARTIFACT):
        assert len(artifact.executable_sha256_digest) == 64
        int(artifact.executable_sha256_digest, 16)


def test_artifact_executable_digest_differs_from_archive_digest() -> None:
    for artifact in (WINDOWS_AMD64_ARTIFACT, LINUX_AMD64_ARTIFACT):
        assert artifact.executable_sha256_digest != artifact.sha256_digest
