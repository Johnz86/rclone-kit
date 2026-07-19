"""Resolver that owns the lifetime of the rclone executable used at runtime.

Callers must not repeat search, download, permission, or cache logic; every
public entry point in this module returns an absolute, verified `Path` or
raises a documented exception. The resolver never moves a binary into
`/usr/local/bin`, `~/.local/bin`, or any other system location, and every
subprocess it may transitively invoke (through the downloader) uses argument
lists rather than `shell=True`.
"""

import importlib.resources
import shutil
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

import platformdirs

from rclone_kit.runtime.archive_extract import extract_single_member
from rclone_kit.runtime.downloader import fetch_verified_archive
from rclone_kit.runtime.exceptions import (
    CacheVerificationError,
    ExplicitExecutableNotFoundError,
    RcloneResolutionError,
)
from rclone_kit.runtime.hashing import atomic_replace_file, best_effort_unlink, sha256_of_file
from rclone_kit.runtime.permissions import apply_executable_permission
from rclone_kit.runtime.platform import (
    RCLONE_VERSION,
    RcloneArtifact,
    resolve_artifact_for_running_platform,
)

_CACHE_APPLICATION_NAME = "rclone-kit"
_PACKAGED_ASSETS_PACKAGE_NAME = "rclone_kit"
_PACKAGED_ASSETS_SUBDIRECTORY = ("assets", "rclone")
_EXECUTABLE_MANIFEST_SUFFIX = ".sha256"
_TEMP_FILE_SUFFIX = ".tmp"

_STRATEGY_BUNDLED_ASSET = "bundled_asset"
_STRATEGY_PATH_LOOKUP = "path_lookup"
_STRATEGY_VERIFIED_DOWNLOAD = "verified_download"


def resolve_rclone_executable(
    *,
    explicit_path: Path | None = None,
    artifact: RcloneArtifact | None = None,
    allow_path_lookup: bool = False,
    allow_verified_download: bool = False,
    cache_root: Path | None = None,
    packaged_assets_root: Path | None = None,
) -> Path:
    """Resolve an absolute `Path` to a usable rclone executable.

    Resolution order:

    1. `explicit_path`, when given: validated to exist and be a file. Raises
       `ExplicitExecutableNotFoundError` otherwise. Never falls through to
       later strategies, since an explicit path is an authoritative override.
    2. The executable bundled with the installed wheel under
       `packaged_assets_root/<wheel_platform_tag>/<executable_name>`,
       verified against its sibling `.sha256` manifest and materialized into
       the `cache_root` application cache (obtained through `platformdirs`
       when `cache_root` is not given).
    3. A `PATH` lookup via `shutil.which`, only when `allow_path_lookup` is
       `True`.
    4. A verified download fallback, only when `allow_verified_download` is
       `True`.

    `artifact` defaults to `resolve_artifact_for_running_platform()`.

    Raises `RcloneResolutionError` when every enabled strategy fails.
    """
    if explicit_path is not None:
        return _validate_explicit_path(explicit_path)

    resolved_artifact = (
        artifact if artifact is not None else resolve_artifact_for_running_platform()
    )
    resolved_cache_root = cache_root if cache_root is not None else default_cache_root()
    resolved_assets_root = (
        packaged_assets_root if packaged_assets_root is not None else default_packaged_assets_root()
    )

    attempted_strategies: list[str] = [_STRATEGY_BUNDLED_ASSET]
    bundled = _try_bundled_asset(resolved_artifact, resolved_assets_root, resolved_cache_root)
    if bundled is not None:
        return bundled

    if allow_path_lookup:
        attempted_strategies.append(_STRATEGY_PATH_LOOKUP)
        found_on_path = shutil.which(resolved_artifact.executable_name)
        if found_on_path is not None:
            return Path(found_on_path).resolve()

    if allow_verified_download:
        attempted_strategies.append(_STRATEGY_VERIFIED_DOWNLOAD)
        return _install_via_verified_download(resolved_artifact, resolved_cache_root)

    raise RcloneResolutionError(attempted_strategies)


def default_cache_root() -> Path:
    """Return the default application cache root for bundled rclone
    executables, versioned by `RCLONE_VERSION` so an upgrade cannot collide
    with a previously cached executable.
    """
    return Path(platformdirs.user_cache_dir(_CACHE_APPLICATION_NAME)) / "rclone" / RCLONE_VERSION


def default_packaged_assets_root() -> Path:
    """Return the default directory under the installed `rclone_kit` package
    where per-platform rclone executables are staged as package data.

    Assumes the package is installed as ordinary files on disk, which holds
    for wheel installs; a namespace or zipimport install has no matching
    directory and the bundled-asset strategy simply finds nothing there.
    """
    package_root = Path(str(importlib.resources.files(_PACKAGED_ASSETS_PACKAGE_NAME)))
    for segment in _PACKAGED_ASSETS_SUBDIRECTORY:
        package_root = package_root / segment
    return package_root


def _validate_explicit_path(path: Path) -> Path:
    if not path.is_file():
        raise ExplicitExecutableNotFoundError(path)
    return path.resolve()


def _try_bundled_asset(
    artifact: RcloneArtifact, assets_root: Path, cache_root: Path
) -> Path | None:
    packaged_executable = assets_root / artifact.wheel_platform_tag / artifact.executable_name
    manifest_path = packaged_executable.with_name(
        packaged_executable.name + _EXECUTABLE_MANIFEST_SUFFIX
    )
    if not packaged_executable.is_file() or not manifest_path.is_file():
        return None
    expected_digest = _read_manifest_digest(manifest_path)
    cache_path = cache_root / artifact.wheel_platform_tag / artifact.executable_name

    def populate(temp_path: Path) -> None:
        shutil.copyfile(packaged_executable, temp_path)

    return _install_into_cache(cache_path, expected_digest, populate, artifact)


def _install_via_verified_download(artifact: RcloneArtifact, cache_root: Path) -> Path:
    cache_path = cache_root / artifact.wheel_platform_tag / artifact.executable_name
    with TemporaryDirectory() as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)
        archive_path = temp_dir / artifact.archive_filename
        fetch_verified_archive(artifact, archive_path)
        extracted_path = temp_dir / artifact.executable_name
        extract_single_member(archive_path, artifact.executable_member_name, extracted_path)
        expected_digest = sha256_of_file(extracted_path)

        def populate(temp_path: Path) -> None:
            shutil.copyfile(extracted_path, temp_path)

        return _install_into_cache(cache_path, expected_digest, populate, artifact)


def _install_into_cache(
    cache_path: Path,
    expected_digest: str,
    populate: Callable[[Path], None],
    artifact: RcloneArtifact,
) -> Path:
    """Materialize a verified executable at `cache_path`, reusing an already
    valid cache entry and atomically replacing an invalid one.

    `populate` writes the candidate executable bytes to the temporary path it
    is given. Raises `CacheVerificationError` when the populated content does
    not match `expected_digest`, and `CacheReplacementError` (propagated from
    `atomic_replace_file`) when the atomic replacement itself fails.
    """
    if cache_path.is_file() and sha256_of_file(cache_path) == expected_digest:
        return cache_path

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(cache_path.name + _TEMP_FILE_SUFFIX)
    populate(temp_path)
    actual_digest = sha256_of_file(temp_path)
    if actual_digest != expected_digest:
        best_effort_unlink(temp_path)
        raise CacheVerificationError(cache_path, expected_digest, actual_digest)
    apply_executable_permission(temp_path, artifact)
    atomic_replace_file(temp_path, cache_path)
    return cache_path


def _read_manifest_digest(manifest_path: Path) -> str:
    return manifest_path.read_text(encoding="utf-8").strip()
