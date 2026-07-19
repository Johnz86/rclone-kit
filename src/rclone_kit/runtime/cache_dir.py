"""Manual per-user application cache-directory resolver.

This project only certifies Windows and Linux (see
`rclone_kit.runtime.platform.OperatingSystem` and `SUPPORTED_ARTIFACTS`), so
the cross-platform generality of a third-party library like `platformdirs`
(which also handles macOS, the various BSDs, etc.) is unneeded weight. This
module implements only the two conventions this project actually needs:

- Windows: `%LOCALAPPDATA%\\<app_name>`, falling back to
  `~\\AppData\\Local\\<app_name>` when the `LOCALAPPDATA` environment
  variable is unset.
- Linux: `$XDG_CACHE_HOME/<app_name>` per the XDG Base Directory
  specification, falling back to `~/.cache/<app_name>` when
  `XDG_CACHE_HOME` is unset.
"""

import os
import platform as _platform
from pathlib import Path

from rclone_kit.runtime.platform import OperatingSystem, normalize_operating_system

_LOCALAPPDATA_ENV_VAR = "LOCALAPPDATA"
_WINDOWS_FALLBACK_SEGMENTS = ("AppData", "Local")

_XDG_CACHE_HOME_ENV_VAR = "XDG_CACHE_HOME"
_LINUX_FALLBACK_SEGMENTS = (".cache",)


def user_cache_dir(app_name: str, *, operating_system: OperatingSystem | None = None) -> Path:
    """Return the per-user application cache directory for `app_name`.

    `operating_system` defaults to the currently running platform's
    `OperatingSystem`, resolved from `platform.system()` the same way
    `rclone_kit.runtime.platform.resolve_artifact_for_running_platform`
    resolves an artifact. It is accepted as an explicit parameter so tests
    can pass `OperatingSystem.WINDOWS` / `OperatingSystem.LINUX` directly
    instead of monkeypatching `platform.system()` globally, matching this
    project's platform-detection testing convention (see
    `resolve_rclone_artifact`).

    Raises `UnsupportedPlatformError` when `operating_system` is not given
    and the running platform has no certified mapping.
    """
    resolved_os = (
        operating_system
        if operating_system is not None
        else normalize_operating_system(_platform.system())
    )
    if resolved_os is OperatingSystem.WINDOWS:
        return _windows_cache_dir(app_name)
    return _linux_cache_dir(app_name)


def _windows_cache_dir(app_name: str) -> Path:
    local_app_data = os.environ.get(_LOCALAPPDATA_ENV_VAR)
    base = (
        Path(local_app_data)
        if local_app_data
        else Path.home().joinpath(*_WINDOWS_FALLBACK_SEGMENTS)
    )
    return base / app_name


def _linux_cache_dir(app_name: str) -> Path:
    xdg_cache_home = os.environ.get(_XDG_CACHE_HOME_ENV_VAR)
    base = (
        Path(xdg_cache_home) if xdg_cache_home else Path.home().joinpath(*_LINUX_FALLBACK_SEGMENTS)
    )
    return base / app_name
