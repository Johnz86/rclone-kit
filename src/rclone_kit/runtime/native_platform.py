"""Data-driven platform model for the certified native (C ABI) build targets.

Describes the rclone-kit-owned `librclone_kit` shared library and its
companion diagnostic executable, both built from the pinned `native/rclone`
submodule. `scripts/native/build.py` and the native library resolver
(`rclone_kit.native.library`) import from here instead of repeating
filenames or build-tag lists.
"""

from dataclasses import dataclass

from rclone_kit.runtime.exceptions import UnsupportedPlatformError
from rclone_kit.runtime.platform import (
    MachineArchitecture,
    OperatingSystem,
    normalize_machine_architecture,
    normalize_operating_system,
)


@dataclass(frozen=True)
class NativeTarget:
    """Immutable description of one certified native build target.

    `go_build_tags` is empty for the no-mount development profile. The
    production profile adds `cmount` on Windows only, where it selects
    cgofuse's WinFsp path; Linux mount support (`cmd/mount`) needs no build
    tag - see `scripts/native/build.py`'s `_build_tags`.
    """

    operating_system: OperatingSystem
    architecture: MachineArchitecture
    wheel_platform_tag: str
    executable_filename: str
    library_filename: str
    go_build_tags: tuple[str, ...]


WINDOWS_AMD64_NATIVE_TARGET = NativeTarget(
    operating_system=OperatingSystem.WINDOWS,
    architecture=MachineArchitecture.AMD64,
    wheel_platform_tag="win_amd64",
    executable_filename="rclone.exe",
    library_filename="librclone_kit.dll",
    go_build_tags=(),
)

LINUX_AMD64_NATIVE_TARGET = NativeTarget(
    operating_system=OperatingSystem.LINUX,
    architecture=MachineArchitecture.AMD64,
    wheel_platform_tag="manylinux2014_x86_64",
    executable_filename="rclone",
    library_filename="librclone_kit.so",
    go_build_tags=(),
)

SUPPORTED_NATIVE_TARGETS: tuple[NativeTarget, ...] = (
    WINDOWS_AMD64_NATIVE_TARGET,
    LINUX_AMD64_NATIVE_TARGET,
)

_NATIVE_TARGETS_BY_PLATFORM: dict[tuple[OperatingSystem, MachineArchitecture], NativeTarget] = {
    (target.operating_system, target.architecture): target for target in SUPPORTED_NATIVE_TARGETS
}


def resolve_native_target(system: str, machine: str) -> NativeTarget:
    """Resolve the certified `NativeTarget` for explicit platform values.

    `system` and `machine` follow the raw string shapes returned by
    `platform.system()` and `platform.machine()` respectively (matching is
    case-insensitive, delegated to `rclone_kit.runtime.platform`'s
    normalizers so both artifact models agree on what a platform string
    means).

    Raises `UnsupportedPlatformError` when the combination has no certified
    native build target.
    """
    operating_system = normalize_operating_system(system)
    architecture = normalize_machine_architecture(machine)
    target = _NATIVE_TARGETS_BY_PLATFORM.get((operating_system, architecture))
    if target is None:
        raise UnsupportedPlatformError(system=system, machine=machine)
    return target


def native_target_choices() -> tuple[str, ...]:
    """Return every `<os>-<arch>` target string accepted by `--target`
    command-line arguments, derived from `SUPPORTED_NATIVE_TARGETS` so
    scripts never hardcode a second platform table.
    """
    return tuple(
        f"{target.operating_system.value}-{target.architecture.value}"
        for target in SUPPORTED_NATIVE_TARGETS
    )
