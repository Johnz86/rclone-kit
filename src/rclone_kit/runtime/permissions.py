"""Executable permission handling shared by the runtime cache installer and
the build-time artifact preparation script.
"""

import os
from pathlib import Path

from rclone_kit.runtime.platform import OperatingSystem, RcloneArtifact

LINUX_EXECUTABLE_PERMISSION_BITS = 0o755


def apply_executable_permission(path: Path, artifact: RcloneArtifact) -> None:
    """Apply the owner/group/other executable permission bit to `path` when
    `artifact` targets Linux.

    A no-op on Windows, which has no equivalent permission bit.
    """
    if artifact.operating_system is OperatingSystem.LINUX:
        os.chmod(path, LINUX_EXECUTABLE_PERMISSION_BITS)
