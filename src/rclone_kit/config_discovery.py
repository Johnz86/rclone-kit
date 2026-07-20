"""Rclone configuration-file discovery and config-path parsing."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rclone_kit.runtime.exceptions import RcloneRuntimeError
from rclone_kit.util import get_rclone_exe

_RCLONE_CONFIG_ENV_VAR = "RCLONE_CONFIG"
_CONFIG_PATHS_COMMAND = ("config", "paths")
_CONFIG_FILE_LABEL = "config file"
_CACHE_DIR_LABEL = "cache dir"
_TEMP_DIR_LABEL = "temp dir"


class ConfigDiscoveryError(Exception):
    """Raised when configuration discovery fails instead of finding no file."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Failed to discover the rclone config file: {detail}")


@dataclass(frozen=True)
class RclonePaths:
    """Filesystem locations reported by ``rclone config paths``."""

    config_file: Path | None
    cache_dir: Path | None
    temp_dir: Path | None

    def present_paths(self) -> list[Path]:
        """Return reported paths in rclone's stable display order."""
        return [path for path in (self.config_file, self.cache_dir, self.temp_dir) if path]


def parse_rclone_paths(stdout: str) -> RclonePaths:
    """Parse labeled config paths while preserving Windows drive colons."""
    values: dict[str, Path] = {}
    for line in stdout.splitlines():
        label, separator, value = line.partition(":")
        if separator and value.strip():
            values[label.strip().lower()] = Path(value.strip())
    return RclonePaths(
        config_file=values.get(_CONFIG_FILE_LABEL),
        cache_dir=values.get(_CACHE_DIR_LABEL),
        temp_dir=values.get(_TEMP_DIR_LABEL),
    )


def find_conf_file(
    *,
    explicit_path: Path | None = None,
    rclone_exe: Path | None = None,
) -> Path | None:
    """Find a config via explicit path, environment, then rclone itself."""
    if explicit_path is not None:
        return explicit_path

    if env_value := os.environ.get(_RCLONE_CONFIG_ENV_VAR):
        return Path(env_value)

    config_file = _config_paths_via_executable(rclone_exe).config_file
    if config_file is not None and config_file.exists():
        return config_file
    return None


def _config_paths_via_executable(rclone_exe: Path | None) -> RclonePaths:
    """Invoke ``rclone config paths`` through an explicit or resolved binary."""
    try:
        executable = get_rclone_exe(rclone_exe)
    except RcloneRuntimeError as error:
        raise ConfigDiscoveryError("resolve an rclone executable") from error

    try:
        completed = subprocess.run(
            [str(executable), *_CONFIG_PATHS_COMMAND],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ConfigDiscoveryError(f"invoke {executable} config paths") from error

    return parse_rclone_paths(completed.stdout)
