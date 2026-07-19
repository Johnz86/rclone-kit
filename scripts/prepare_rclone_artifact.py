"""Build-time staging of one verified rclone release artifact.

Downloads the certified rclone archive for an explicit `(target-os,
target-arch)` pair, verifies its SHA-256 against the repository-controlled
digest declared in `rclone_kit.runtime.platform` (the same digest the
runtime resolver's download fallback verifies against), and extracts only
the expected executable into a generated output directory alongside its
SHA-256 manifest and the vendored rclone MIT license text.

This script never writes into a tracked source folder; `--out-dir` must be a
build directory such as `build/rclone-artifacts`. A later packaging step
copies the staged `<out-dir>/rclone/<wheel-platform-tag>/` directory into
`src/rclone_kit/assets/rclone/<wheel-platform-tag>/` before `uv build` runs.

Usage:
    uv run python scripts/prepare_rclone_artifact.py windows amd64 --out-dir build/rclone-artifacts
    uv run python scripts/prepare_rclone_artifact.py linux amd64 --out-dir build/rclone-artifacts
"""

import argparse
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from rclone_kit.runtime.archive_extract import extract_single_member
from rclone_kit.runtime.downloader import fetch_verified_archive
from rclone_kit.runtime.exceptions import RcloneRuntimeError
from rclone_kit.runtime.hashing import sha256_of_file
from rclone_kit.runtime.permissions import apply_executable_permission
from rclone_kit.runtime.platform import (
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
    return parser.parse_args(argv)


def _staging_directory(out_dir: Path, artifact: RcloneArtifact) -> Path:
    return out_dir / _STAGING_SUBDIRECTORY / artifact.wheel_platform_tag


def _stage_executable(artifact: RcloneArtifact, staging_directory: Path) -> Path:
    executable_path = staging_directory / artifact.executable_name
    with TemporaryDirectory() as raw_temp_dir:
        archive_path = Path(raw_temp_dir) / artifact.archive_filename
        fetch_verified_archive(artifact, archive_path)
        extract_single_member(archive_path, artifact.executable_member_name, executable_path)
    apply_executable_permission(executable_path, artifact)
    _write_manifest(executable_path)
    return executable_path


def _write_manifest(executable_path: Path) -> None:
    digest = sha256_of_file(executable_path)
    manifest_path = executable_path.with_name(executable_path.name + _STAGED_MANIFEST_SUFFIX)
    manifest_path.write_text(digest, encoding="utf-8")


def _stage_license(staging_directory: Path) -> Path:
    license_path = staging_directory / _STAGED_LICENSE_FILENAME
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
        staging_directory = _staging_directory(args.out_dir, artifact)
        staging_directory.mkdir(parents=True, exist_ok=True)
        executable_path = _stage_executable(artifact, staging_directory)
        license_path = _stage_license(staging_directory)
    except RcloneRuntimeError as error:
        print(f"Failed to prepare rclone artifact: {error}", file=sys.stderr)
        return 1
    print(f"Staged executable: {executable_path}")
    print(f"Staged license: {license_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
