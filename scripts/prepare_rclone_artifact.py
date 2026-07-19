"""Build-time staging of one verified rclone release artifact.

Downloads the certified rclone archive for an explicit `(target-os,
target-arch)` pair, verifies its SHA-256 against the repository-controlled
digest declared in `rclone_kit.runtime.platform` (the same digest the
runtime resolver's download fallback verifies against), and extracts only
the expected executable into a generated output directory alongside its
SHA-256 manifest and the vendored rclone MIT license text. The extracted
executable's digest is verified against
`RcloneArtifact.executable_sha256_digest` immediately, before it is written
into the staging directory, so a corrupted extraction is never staged.

This script never writes into a tracked source folder; `--out-dir` must be a
build directory such as `build/rclone-artifacts`. `scripts/build_distribution.py`
is the canonical caller: it stages through this module's functions directly
and copies the result into a temporary wheel-build source tree.

The downloaded archive itself (not the extracted executable) is cached
under a per-user, per-`RCLONE_VERSION`, per-platform directory, keyed
implicitly by `RcloneArtifact.sha256_digest`: a cached archive is only
reused after `fetch_verified_archive` recomputes and confirms its digest,
so a corrupted or stale cache entry is transparently redownloaded rather
than trusted. This avoids repeating a multi-megabyte download across
repeated local builds or CI runs for the same certified rclone release.

Usage:
    uv run python scripts/prepare_rclone_artifact.py windows amd64 --out-dir build/rclone-artifacts
    uv run python scripts/prepare_rclone_artifact.py linux amd64 --out-dir build/rclone-artifacts
"""

import argparse
import shutil
import sys
from pathlib import Path

from rclone_kit.runtime.archive_extract import extract_single_member
from rclone_kit.runtime.cache_dir import user_cache_dir
from rclone_kit.runtime.downloader import fetch_verified_archive
from rclone_kit.runtime.exceptions import RcloneRuntimeError, StagedExecutableDigestMismatchError
from rclone_kit.runtime.hashing import sha256_of_file
from rclone_kit.runtime.permissions import apply_executable_permission
from rclone_kit.runtime.platform import (
    RCLONE_VERSION,
    MachineArchitecture,
    OperatingSystem,
    RcloneArtifact,
    resolve_rclone_artifact,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RCLONE_LICENSE_SOURCE = _REPO_ROOT / "licenses" / "rclone" / "COPYING"
_STAGED_LICENSE_FILENAME = "RCLONE_LICENSE"
_STAGED_MANIFEST_SUFFIX = ".sha256"
_STAGING_SUBDIRECTORY = "rclone"
_CACHE_APPLICATION_NAME = "rclone-kit"
_ARCHIVE_CACHE_SUBDIRECTORY = "archives"


def default_archive_cache_root() -> Path:
    """Return the default per-user directory that cached, verified rclone
    release archives are stored under.
    """
    return user_cache_dir(_CACHE_APPLICATION_NAME) / _ARCHIVE_CACHE_SUBDIRECTORY


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target_os",
        choices=[operating_system.value for operating_system in OperatingSystem],
        help="Certified target operating system.",
    )
    parser.add_argument(
        "target_arch",
        choices=[architecture.value for architecture in MachineArchitecture],
        help="Certified target machine architecture.",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        type=Path,
        help="Generated build directory to stage the artifact into.",
    )
    parser.add_argument(
        "--archive-cache-dir",
        type=Path,
        default=None,
        help=(
            "Directory to cache the verified downloaded archive in, reused "
            "across runs. Defaults to a per-user cache directory."
        ),
    )
    return parser.parse_args(argv)


def staging_directory(out_dir: Path, artifact: RcloneArtifact) -> Path:
    """Return the `<out-dir>/rclone/<wheel-platform-tag>/` directory that
    `stage_executable` and `stage_license` populate for `artifact`.
    """
    return out_dir / _STAGING_SUBDIRECTORY / artifact.wheel_platform_tag


def stage_executable(
    artifact: RcloneArtifact, staging_dir: Path, *, archive_cache_dir: Path | None = None
) -> Path:
    """Download (or reuse a cached, verified download of) `artifact`'s
    archive, then extract its executable into `staging_dir`, alongside a
    `.sha256` manifest.

    Raises `StagedExecutableDigestMismatchError` when the freshly extracted
    executable's digest disagrees with `artifact.executable_sha256_digest`;
    a mismatched executable is never left behind in `staging_dir`.
    """
    executable_path = staging_dir / artifact.executable_name
    cache_root = (
        archive_cache_dir if archive_cache_dir is not None else default_archive_cache_root()
    )
    archive_path = _cached_verified_archive(artifact, cache_root)
    extract_single_member(archive_path, artifact.executable_member_name, executable_path)
    apply_executable_permission(executable_path, artifact)
    _verify_staged_digest(artifact, executable_path)
    _write_manifest(executable_path)
    return executable_path


def _cached_verified_archive(artifact: RcloneArtifact, cache_root: Path) -> Path:
    """Return a local path to `artifact`'s archive, downloading it only when
    no valid cache entry already exists.

    A cache hit still recomputes the cached file's digest before reuse — see
    `fetch_verified_archive`'s early-return path, keyed to the caching
    convention below — so a corrupted or stale cache entry is
    transparently redownloaded rather than trusted blindly.
    """
    cache_path = (
        cache_root / RCLONE_VERSION / artifact.wheel_platform_tag / artifact.archive_filename
    )
    if cache_path.is_file() and sha256_of_file(cache_path) == artifact.sha256_digest:
        return cache_path
    return fetch_verified_archive(artifact, cache_path)


def _verify_staged_digest(artifact: RcloneArtifact, executable_path: Path) -> None:
    actual_digest = sha256_of_file(executable_path)
    if actual_digest != artifact.executable_sha256_digest:
        executable_path.unlink()
        raise StagedExecutableDigestMismatchError(
            executable_path, artifact.executable_sha256_digest, actual_digest
        )


def _write_manifest(executable_path: Path) -> None:
    digest = sha256_of_file(executable_path)
    manifest_path = executable_path.with_name(executable_path.name + _STAGED_MANIFEST_SUFFIX)
    manifest_path.write_text(digest, encoding="utf-8")


def stage_license(staging_dir: Path) -> Path:
    """Copy the vendored rclone MIT license text into `staging_dir`."""
    license_path = staging_dir / _STAGED_LICENSE_FILENAME
    shutil.copyfile(_RCLONE_LICENSE_SOURCE, license_path)
    return license_path


def main(argv: list[str] | None = None) -> int:
    """Stage one verified rclone build target into `--out-dir`.

    Returns 0 on success. Returns 1 and prints a diagnostic to stderr when
    platform resolution, download, digest verification, or extraction fails.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        artifact = resolve_rclone_artifact(system=args.target_os, machine=args.target_arch)
        target_staging_directory = staging_directory(args.out_dir, artifact)
        target_staging_directory.mkdir(parents=True, exist_ok=True)
        executable_path = stage_executable(
            artifact, target_staging_directory, archive_cache_dir=args.archive_cache_dir
        )
        license_path = stage_license(target_staging_directory)
    except RcloneRuntimeError as error:
        print(f"Failed to prepare rclone artifact: {error}", file=sys.stderr)
        return 1
    print(f"Staged executable: {executable_path}")
    print(f"Staged license: {license_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
