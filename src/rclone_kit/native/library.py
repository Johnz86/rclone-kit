"""Native library path resolution.

Resolution order, per `reference/rclone_c_abi_implementation_plan.md`'s
"Library loading" section:

1. an explicit `library_path` passed by the caller;
2. the `RCLONE_KIT_LIBRARY` development override;
3. a packaged wheel asset, verified against its sibling
   `native-manifest.json`'s own recorded digest.

Always returns an absolute path to an existing file; never searches `PATH`
or the current working directory.
"""

import importlib.resources
import json
import os
import platform
from pathlib import Path

from rclone_kit.native.errors import LibraryNotFoundError, LibraryVerificationError
from rclone_kit.runtime.exceptions import UnsupportedPlatformError
from rclone_kit.runtime.hashing import sha256_of_file
from rclone_kit.runtime.native_platform import NativeTarget, resolve_native_target

RCLONE_KIT_LIBRARY_ENV_VAR = "RCLONE_KIT_LIBRARY"

_PACKAGED_ASSETS_PACKAGE_NAME = "rclone_kit"
_PACKAGED_ASSETS_SUBDIRECTORY = ("assets", "native")
_MANIFEST_FILENAME = "native-manifest.json"


def resolve_library_path(
    explicit_path: Path | None = None,
    *,
    native_target: NativeTarget | None = None,
    packaged_assets_root: Path | None = None,
) -> Path:
    """Resolve an absolute path to the native `librclone_kit` library.

    Resolution order:

    1. `explicit_path`, when given: validated to exist and be a file. Raises
       `LibraryNotFoundError` otherwise, without falling through to later
       strategies, since an explicit path is an authoritative override.
    2. The `RCLONE_KIT_LIBRARY` environment variable, when set: validated the
       same way.
    3. The library bundled with the installed wheel under
       `packaged_assets_root/<wheel_platform_tag>/<library_filename>`,
       verified against its sibling `native-manifest.json`'s own recorded
       digest for that filename, then loaded directly from the installed
       package location - no cache-directory copy is needed the way the
       downloaded rclone executable resolver (`runtime.rclone_binary`) needs
       one, since a shared library needs no executable-permission flag and
       `ctypes.CDLL` loads it in place.

    `native_target` defaults to the running platform's certified
    `NativeTarget` (`resolve_native_target(system=platform.system(),
    machine=platform.machine())`); an unsupported platform is treated the
    same as "no packaged asset available" rather than raised directly, so
    the final `LibraryNotFoundError` message stays uniform.

    Raises `LibraryVerificationError` when a packaged asset's digest
    disagrees with its manifest - a mismatched library is never loaded.
    """
    if explicit_path is not None:
        resolved = explicit_path.resolve()
        if not resolved.is_file():
            raise LibraryNotFoundError(resolved)
        return resolved

    env_value = os.environ.get(RCLONE_KIT_LIBRARY_ENV_VAR)
    if env_value:
        resolved = Path(env_value).resolve()
        if not resolved.is_file():
            raise LibraryNotFoundError(resolved)
        return resolved

    resolved_target = (
        native_target if native_target is not None else _resolve_running_native_target()
    )
    resolved_assets_root = (
        packaged_assets_root if packaged_assets_root is not None else default_packaged_assets_root()
    )
    bundled = _try_bundled_asset(resolved_target, resolved_assets_root)
    if bundled is not None:
        return bundled

    raise LibraryNotFoundError(None)


def _resolve_running_native_target() -> NativeTarget | None:
    try:
        return resolve_native_target(system=platform.system(), machine=platform.machine())
    except UnsupportedPlatformError:
        return None


def default_packaged_assets_root() -> Path:
    """Return the default directory under the installed `rclone_kit` package
    where per-platform native libraries are staged as package data.

    Assumes the package is installed as ordinary files on disk, which holds
    for wheel installs; a namespace or zipimport install has no matching
    directory and the bundled-asset strategy simply finds nothing there.
    """
    package_root = Path(str(importlib.resources.files(_PACKAGED_ASSETS_PACKAGE_NAME)))
    for segment in _PACKAGED_ASSETS_SUBDIRECTORY:
        package_root = package_root / segment
    return package_root


def _try_bundled_asset(native_target: NativeTarget | None, assets_root: Path) -> Path | None:
    if native_target is None:
        return None
    packaged_library = (
        assets_root / native_target.wheel_platform_tag / native_target.library_filename
    ).resolve()
    manifest_path = packaged_library.with_name(_MANIFEST_FILENAME)
    if not packaged_library.is_file() or not manifest_path.is_file():
        return None
    expected_digest = _manifest_digest_for(manifest_path, native_target.library_filename)
    if expected_digest is None:
        return None
    actual_digest = sha256_of_file(packaged_library)
    if actual_digest != expected_digest:
        raise LibraryVerificationError(packaged_library, expected_digest, actual_digest)
    return packaged_library


def _manifest_digest_for(manifest_path: Path, filename: str) -> str | None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for output in manifest.get("outputs", []):
        if output.get("filename") == filename:
            digest = output.get("sha256_digest")
            return digest if isinstance(digest, str) else None
    return None
