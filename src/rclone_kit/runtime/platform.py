"""Data-driven platform model for the certified rclone release builds.

This module is the single source of truth for the rclone version, the
supported operating systems and machine architectures, and the immutable
`RcloneArtifact` describing each certified build target. Both the runtime
executable resolver and the build-time artifact preparation script import
from here instead of repeating version strings, URLs, or digests.
"""

import platform as _platform
from dataclasses import dataclass
from enum import Enum, unique

from rclone_kit.runtime.exceptions import UnsupportedPlatformError

RCLONE_VERSION = "1.74.4"
"""The certified rclone release. Bumping this is a reviewable change that
must update every URL, digest, and test that depends on it."""

RCLONE_RELEASE_TAG = f"v{RCLONE_VERSION}"

_RCLONE_DOWNLOAD_BASE_URL = "https://downloads.rclone.org"


@unique
class OperatingSystem(Enum):
    """A supported target operating system."""

    WINDOWS = "windows"
    LINUX = "linux"


@unique
class MachineArchitecture(Enum):
    """A supported target machine architecture."""

    AMD64 = "amd64"


@dataclass(frozen=True)
class RcloneArtifact:
    """Immutable description of one certified rclone release build target.

    `sha256_digest` is the expected digest of the downloadable archive itself
    (`archive_filename`), sourced from the signed upstream
    `SHA256SUMS` document for `RCLONE_RELEASE_TAG`, not the digest of the
    extracted executable.

    `executable_sha256_digest` is the expected digest of the extracted
    executable named by `executable_member_name`, independently reproduced by
    hashing the executable extracted from a freshly verified download. A
    packaged wheel ships its own `<executable>.sha256` manifest, written at
    build time by `scripts/prepare_rclone_artifact.py`; `executable_sha256_digest`
    is the repository-controlled value that manifest must agree with, so
    `scripts/verify_distribution.py` can detect a corrupted executable and a
    stale-but-internally-consistent manifest as distinct failures.
    """

    operating_system: OperatingSystem
    architecture: MachineArchitecture
    archive_filename: str
    download_url: str
    sha256_digest: str
    executable_member_name: str
    executable_name: str
    executable_sha256_digest: str
    wheel_platform_tag: str


def _archive_filename(os_slug: str, arch_slug: str) -> str:
    return f"rclone-{RCLONE_RELEASE_TAG}-{os_slug}-{arch_slug}.zip"


def _download_url(archive_filename: str) -> str:
    return f"{_RCLONE_DOWNLOAD_BASE_URL}/{RCLONE_RELEASE_TAG}/{archive_filename}"


def _archive_member_name(archive_stem: str, member_filename: str) -> str:
    return f"{archive_stem}/{member_filename}"


_WINDOWS_AMD64_ARCHIVE_STEM = f"rclone-{RCLONE_RELEASE_TAG}-windows-amd64"
_LINUX_AMD64_ARCHIVE_STEM = f"rclone-{RCLONE_RELEASE_TAG}-linux-amd64"

_WINDOWS_AMD64_ARCHIVE_FILENAME = f"{_WINDOWS_AMD64_ARCHIVE_STEM}.zip"
_LINUX_AMD64_ARCHIVE_FILENAME = f"{_LINUX_AMD64_ARCHIVE_STEM}.zip"


WINDOWS_AMD64_SHA256_DIGEST = "ef097ef9de37a57feb7d9f9c7afb34148ad3c65be8025f1d8f7f521554a701ea"
LINUX_AMD64_SHA256_DIGEST = "fe435e0c36228e7c2f116a8701f01127bb1f694005fc11d1f27186c8bca4115d"


WINDOWS_AMD64_EXECUTABLE_SHA256_DIGEST = (
    "492648a3867dbc620188a305e05ff3216aecbf4622bf1a6b5b978ed9c939e18c"
)
LINUX_AMD64_EXECUTABLE_SHA256_DIGEST = (
    "9f56ca5edfac24a3ed37226c2ba1de69f1ec9e05fa2526cddee5cd97e202be6b"
)

WINDOWS_AMD64_ARTIFACT = RcloneArtifact(
    operating_system=OperatingSystem.WINDOWS,
    architecture=MachineArchitecture.AMD64,
    archive_filename=_WINDOWS_AMD64_ARCHIVE_FILENAME,
    download_url=_download_url(_WINDOWS_AMD64_ARCHIVE_FILENAME),
    sha256_digest=WINDOWS_AMD64_SHA256_DIGEST,
    executable_member_name=_archive_member_name(_WINDOWS_AMD64_ARCHIVE_STEM, "rclone.exe"),
    executable_name="rclone.exe",
    executable_sha256_digest=WINDOWS_AMD64_EXECUTABLE_SHA256_DIGEST,
    wheel_platform_tag="win_amd64",
)

LINUX_AMD64_ARTIFACT = RcloneArtifact(
    operating_system=OperatingSystem.LINUX,
    architecture=MachineArchitecture.AMD64,
    archive_filename=_LINUX_AMD64_ARCHIVE_FILENAME,
    download_url=_download_url(_LINUX_AMD64_ARCHIVE_FILENAME),
    sha256_digest=LINUX_AMD64_SHA256_DIGEST,
    executable_member_name=_archive_member_name(_LINUX_AMD64_ARCHIVE_STEM, "rclone"),
    executable_name="rclone",
    executable_sha256_digest=LINUX_AMD64_EXECUTABLE_SHA256_DIGEST,
    wheel_platform_tag="manylinux2014_x86_64",
)

SUPPORTED_ARTIFACTS: tuple[RcloneArtifact, ...] = (WINDOWS_AMD64_ARTIFACT, LINUX_AMD64_ARTIFACT)

_ARTIFACTS_BY_TARGET: dict[tuple[OperatingSystem, MachineArchitecture], RcloneArtifact] = {
    (artifact.operating_system, artifact.architecture): artifact for artifact in SUPPORTED_ARTIFACTS
}

_PLATFORM_SYSTEM_TO_OS: dict[str, OperatingSystem] = {
    "windows": OperatingSystem.WINDOWS,
    "linux": OperatingSystem.LINUX,
}

_PLATFORM_MACHINE_TO_ARCH: dict[str, MachineArchitecture] = {
    "amd64": MachineArchitecture.AMD64,
    "x86_64": MachineArchitecture.AMD64,
}


def normalize_operating_system(system: str) -> OperatingSystem:
    """Map a `platform.system()`-style value to an `OperatingSystem`.

    Raises `UnsupportedPlatformError` when `system` has no certified mapping.
    """
    operating_system = _PLATFORM_SYSTEM_TO_OS.get(system.lower())
    if operating_system is None:
        raise UnsupportedPlatformError(system=system, machine="")
    return operating_system


def normalize_machine_architecture(machine: str) -> MachineArchitecture:
    """Map a `platform.machine()`-style value to a `MachineArchitecture`.

    Raises `UnsupportedPlatformError` when `machine` has no certified mapping.
    """
    architecture = _PLATFORM_MACHINE_TO_ARCH.get(machine.lower())
    if architecture is None:
        raise UnsupportedPlatformError(system="", machine=machine)
    return architecture


def resolve_rclone_artifact(system: str, machine: str) -> RcloneArtifact:
    """Resolve the certified `RcloneArtifact` for explicit platform values.

    `system` and `machine` follow the raw string shapes returned by
    `platform.system()` and `platform.machine()` respectively (matching is
    case-insensitive). Passing explicit values keeps platform detection
    testable at this single boundary instead of patching `platform.system()`
    throughout the codebase.

    Raises `UnsupportedPlatformError` when the combination has no certified
    build target.
    """
    operating_system = _PLATFORM_SYSTEM_TO_OS.get(system.lower())
    architecture = _PLATFORM_MACHINE_TO_ARCH.get(machine.lower())
    if operating_system is None or architecture is None:
        raise UnsupportedPlatformError(system=system, machine=machine)
    artifact = _ARTIFACTS_BY_TARGET.get((operating_system, architecture))
    if artifact is None:
        raise UnsupportedPlatformError(system=system, machine=machine)
    return artifact


def resolve_artifact_for_running_platform() -> RcloneArtifact:
    """Resolve the certified `RcloneArtifact` for the currently running
    process, using `platform.system()` and `platform.machine()`.

    Raises `UnsupportedPlatformError` when the running platform has no
    certified build target.
    """
    return resolve_rclone_artifact(system=_platform.system(), machine=_platform.machine())
