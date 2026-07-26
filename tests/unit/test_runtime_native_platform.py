"""Unit tests for `rclone_kit.runtime.native_platform`."""

from dataclasses import dataclass

import pytest

from rclone_kit.runtime.exceptions import UnsupportedPlatformError
from rclone_kit.runtime.native_platform import (
    LINUX_AMD64_NATIVE_TARGET,
    SUPPORTED_NATIVE_TARGETS,
    WINDOWS_AMD64_NATIVE_TARGET,
    NativeTarget,
    native_target_choices,
    resolve_native_target,
)


@dataclass(frozen=True)
class PlatformMappingCase:
    system: str
    machine: str
    expected_target: NativeTarget


WINDOWS_AMD64_EXACT_CASE = PlatformMappingCase("Windows", "AMD64", WINDOWS_AMD64_NATIVE_TARGET)
WINDOWS_AMD64_LOWERCASE_CASE = PlatformMappingCase("windows", "amd64", WINDOWS_AMD64_NATIVE_TARGET)
LINUX_AMD64_CASE = PlatformMappingCase("Linux", "AMD64", LINUX_AMD64_NATIVE_TARGET)
LINUX_X86_64_CASE = PlatformMappingCase("Linux", "x86_64", LINUX_AMD64_NATIVE_TARGET)

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
def test_resolve_native_target_maps_known_platforms(case: PlatformMappingCase) -> None:
    target = resolve_native_target(system=case.system, machine=case.machine)
    assert target == case.expected_target


def test_resolve_native_target_rejects_unknown_system() -> None:
    with pytest.raises(UnsupportedPlatformError) as excinfo:
        resolve_native_target(system="Darwin", machine="arm64")
    assert excinfo.value.system == "Darwin"


def test_resolve_native_target_rejects_unknown_machine() -> None:
    with pytest.raises(UnsupportedPlatformError):
        resolve_native_target(system="Linux", machine="aarch64")


def test_native_target_choices_match_supported_targets() -> None:
    assert native_target_choices() == tuple(
        f"{target.operating_system.value}-{target.architecture.value}"
        for target in SUPPORTED_NATIVE_TARGETS
    )


def test_native_targets_have_distinct_wheel_platform_tags() -> None:
    assert (
        WINDOWS_AMD64_NATIVE_TARGET.wheel_platform_tag
        != LINUX_AMD64_NATIVE_TARGET.wheel_platform_tag
    )


def test_native_targets_have_distinct_library_filenames() -> None:
    assert (
        WINDOWS_AMD64_NATIVE_TARGET.library_filename != LINUX_AMD64_NATIVE_TARGET.library_filename
    )
