"""Shared fixtures for native-DLL-backed integration tests.

`RcloneKitInitialize` is a once-per-*process* ABI operation - loading the
same shared library path twice via `ctypes.CDLL` within one process returns
a handle to the same already-loaded module and its process-global Go
runtime state - so every test in this directory that needs an initialized
runtime shares the one session-scoped `native_runtime` fixture below instead
of each initializing its own.
"""

import platform as _platform
from collections.abc import Iterator
from pathlib import Path

import pytest

from rclone_kit.native.runtime import RcloneRuntime
from rclone_kit.runtime.exceptions import UnsupportedPlatformError
from rclone_kit.runtime.native_platform import resolve_native_target

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _built_target_dir() -> Path | None:
    try:
        target = resolve_native_target(system=_platform.system(), machine=_platform.machine())
    except UnsupportedPlatformError:
        return None
    candidate = (
        _REPO_ROOT
        / "build"
        / "native"
        / f"{target.operating_system.value}-{target.architecture.value}"
    )
    return candidate if candidate.is_dir() else None


_TARGET_DIR = _built_target_dir()
LIBRARY_PATH: Path | None = _TARGET_DIR / "librclone_kit.dll" if _TARGET_DIR else None
EXECUTABLE_PATH: Path | None = _TARGET_DIR / "rclone.exe" if _TARGET_DIR else None

NATIVE_LIBRARY_AVAILABLE = LIBRARY_PATH is not None and LIBRARY_PATH.is_file()
NATIVE_EXECUTABLE_AVAILABLE = EXECUTABLE_PATH is not None and EXECUTABLE_PATH.is_file()


@pytest.fixture(scope="session")
def native_runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RcloneRuntime]:
    """One `RcloneRuntime`, initialized exactly once for the whole test
    session. Depending tests must not call `initialize()` again themselves.
    """
    assert LIBRARY_PATH is not None
    config_path = tmp_path_factory.mktemp("native-session-runtime") / "rclone.conf"
    rt = RcloneRuntime.from_library_path(LIBRARY_PATH)
    rt.initialize(config_path=config_path)
    yield rt
    rt.close()


@pytest.fixture
def embedded(native_runtime: RcloneRuntime):
    """An `execution="embedded"` client sharing the session's one
    initialized runtime, for CLI-vs-embedded parity tests.
    """
    from rclone_kit.client import Rclone

    return Rclone(None, execution="embedded", runtime=native_runtime)


@pytest.fixture
def cli():
    """The CLI-backed equivalent of `embedded`, built from the exact same
    fork commit (`build/native/<target>/rclone.exe`).
    """
    from rclone_kit.client import Rclone

    assert EXECUTABLE_PATH is not None
    return Rclone(None, rclone_exe=EXECUTABLE_PATH)
