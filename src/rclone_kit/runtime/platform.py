"""Data-driven platform model shared by the certified native build targets.

This module is the single source of truth for the supported operating
systems and machine architectures. `rclone_kit.runtime.native_platform`
imports from here instead of repeating platform-string normalization.
"""

from enum import Enum, unique

from rclone_kit.runtime.exceptions import UnsupportedPlatformError


@unique
class OperatingSystem(Enum):
    """A supported target operating system."""

    WINDOWS = "windows"
    LINUX = "linux"


@unique
class MachineArchitecture(Enum):
    """A supported target machine architecture."""

    AMD64 = "amd64"


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
