"""Shared fixtures for native-DLL-backed integration tests.

`RcloneKitInitialize` is a once-per-*process* ABI operation - loading the
same shared library path twice via `ctypes.CDLL` within one process returns
a handle to the same already-loaded module and its process-global Go
runtime state - so every test in this directory that needs an initialized
runtime shares the one session-scoped `native_runtime` fixture below instead
of each initializing its own.
"""

import json
import platform as _platform
from collections.abc import Iterator
from pathlib import Path

import pytest

from rclone_kit.native.runtime import RcloneRuntime
from rclone_kit.runtime.exceptions import UnsupportedPlatformError
from rclone_kit.runtime.native_platform import NativeTarget, resolve_native_target

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MOUNT_BUILD_TAG = "cmount"


def _resolve_target() -> NativeTarget | None:
    try:
        return resolve_native_target(system=_platform.system(), machine=_platform.machine())
    except UnsupportedPlatformError:
        return None


def _built_target_dir(target: NativeTarget | None) -> Path | None:
    if target is None:
        return None
    candidate = (
        _REPO_ROOT
        / "build"
        / "native"
        / f"{target.operating_system.value}-{target.architecture.value}"
    )
    return candidate if candidate.is_dir() else None


_TARGET = _resolve_target()
_TARGET_DIR = _built_target_dir(_TARGET)
LIBRARY_PATH: Path | None = (
    _TARGET_DIR / _TARGET.library_filename if _TARGET_DIR and _TARGET else None
)

NATIVE_LIBRARY_AVAILABLE = LIBRARY_PATH is not None and LIBRARY_PATH.is_file()


def _built_with_mount_support() -> bool:
    """Whether the currently built target was produced with `--profile
    production` (`-tags cmount`): `mount/mount` only has a real WinFsp/FUSE
    implementation registered in that profile - see
    `native_c_abi_wave_h_review_and_design.md`'s mount addendum. Checked
    via the build's own manifest rather than assumed, since
    `build/native/<target>/` may hold either profile's output depending on
    which `scripts/native/build.py` invocation produced it last.
    """
    if _TARGET_DIR is None:
        return False
    manifest_path = _TARGET_DIR / "native-manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return _MOUNT_BUILD_TAG in manifest.get("go_build_tags", [])


NATIVE_MOUNT_AVAILABLE = NATIVE_LIBRARY_AVAILABLE and _built_with_mount_support()


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
    """An `Rclone` client sharing the session's one initialized runtime."""
    from rclone_kit.client import Rclone

    return Rclone(None, runtime=native_runtime)
