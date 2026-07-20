"""Canonical build orchestration for one certified `rclone-kit` wheel.

Composes the existing staging (`scripts/prepare_rclone_artifact.py`),
verification (`scripts/verify_distribution.py`), and smoke-test
(`scripts/smoke_test_installed_wheel.py`) steps into the single command
`docs/implementation_and_build_pipeline.md` documents, so a caller cannot
produce an unverified or incomplete wheel by skipping a step of an
out-of-band manual sequence. Maintainers and CI both run this script;
neither reproduces the staging/build/verify/smoke-test sequence separately.

Per `docs/implementation_and_build_pipeline.md`'s distribution policy, this
script builds and verifies exactly one platform wheel and never
builds a source distribution: a plain `pip wheel` build from an sdist has no
staging step and would silently produce a wheel without rclone.

Every step after resolving the target runs against an isolated temporary
copy of the source tree, never the tracked checkout, so a successful or
failed build leaves the repository's `src/` byte-identical to before the
build started.

Usage:
    uv run python scripts/build_distribution.py --target windows-amd64 --out-dir dist
    uv run python scripts/build_distribution.py --target linux-amd64
"""

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

import prepare_rclone_artifact
import verify_distribution
from rclone_kit.runtime.exceptions import RcloneRuntimeError
from rclone_kit.runtime.hashing import sha256_of_file
from rclone_kit.runtime.platform import (
    SUPPORTED_ARTIFACTS,
    RcloneArtifact,
    resolve_artifact_for_running_platform,
    resolve_rclone_artifact,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYTHON_VERSION_FILE = _REPO_ROOT / ".python-version"
_SMOKE_TEST_SCRIPT = Path(__file__).resolve().parent / "smoke_test_installed_wheel.py"

_SOURCE_TREE_COPY_ENTRIES: tuple[str, ...] = (
    "pyproject.toml",
    "_build_backend.py",
    "README.md",
    "LICENSE",
    "src",
)
_SOURCE_TREE_IGNORED_DIR_NAMES = frozenset({"__pycache__", "rclone_kit.egg-info"})

_BUILD_ROOT_TEMP_PREFIX = "rclone-kit-build-"
_DIST_TEMP_DIR_PREFIX = "rclone-kit-dist-"
_STAGING_SUBDIRECTORY_NAME = "rclone-artifacts"
_SOURCE_TREE_DIRECTORY_NAME = "source"
_SMOKE_ENV_DIRECTORY_NAME = "smoke-env"
_ASSETS_RELATIVE_DIR = Path("src") / "rclone_kit" / "assets" / "rclone"

_HTTP_PROXY_POISON_URL = "http://127.0.0.1:1"
_HTTP_PROXY_ENV_VAR = "HTTP_PROXY"
_HTTPS_PROXY_ENV_VAR = "HTTPS_PROXY"
_NO_PROXY_ENV_VAR = "NO_PROXY"


class BuildDistributionError(Exception):
    """Raised for an orchestration failure that is not already a documented
    `RcloneRuntimeError` raised by a composed step.
    """


@dataclass(frozen=True)
class BuiltWheel:
    """One verified, smoke-tested wheel produced by `_build_and_verify_wheel`."""

    path: Path
    sha256_digest: str


def _target_choices() -> tuple[str, ...]:
    """Return every `<os>-<arch>` target string accepted by `--target`,
    derived from `SUPPORTED_ARTIFACTS` so this script never hardcodes a
    second platform table.
    """
    return tuple(
        f"{artifact.operating_system.value}-{artifact.architecture.value}"
        for artifact in SUPPORTED_ARTIFACTS
    )


def _resolve_target_artifact(target: str) -> RcloneArtifact:
    system, separator, machine = target.partition("-")
    if not separator:
        raise BuildDistributionError(f"Malformed --target {target!r}; expected '<os>-<arch>'.")
    try:
        return resolve_rclone_artifact(system=system, machine=machine)
    except RcloneRuntimeError as error:
        raise BuildDistributionError(f"Unsupported --target {target!r}: {error}") from error


def _require_running_on_target_platform(artifact: RcloneArtifact) -> None:
    """Fail fast when `artifact` does not match the platform this process is
    running on.

    `_build_backend.py` tags a wheel using the *building host's* platform;
    it does not cross-compile. Requesting a target that does not match the
    running host would otherwise silently produce a mismatched wheel instead
    of failing loudly at the earliest point.
    """
    running_artifact = resolve_artifact_for_running_platform()
    if running_artifact.wheel_platform_tag != artifact.wheel_platform_tag:
        raise BuildDistributionError(
            f"Requested target {artifact.wheel_platform_tag!r} does not match the "
            f"running platform {running_artifact.wheel_platform_tag!r}; this script "
            "does not cross-compile. Run it on a matching host."
        )


def _prepare_output_directory(raw_out_dir: Path | None) -> Path:
    """Return an absolute directory ready to receive the built wheel.

    Returns a freshly created unique temporary directory when `raw_out_dir`
    is `None`. Otherwise returns `raw_out_dir` itself, creating it if
    missing. Raises `BuildDistributionError` when `raw_out_dir` already
    exists and is not empty, so a stale distribution from a prior build can
    never be silently mixed into this one.
    """
    if raw_out_dir is None:
        return Path(mkdtemp(prefix=_DIST_TEMP_DIR_PREFIX))
    if raw_out_dir.exists() and any(raw_out_dir.iterdir()):
        raise BuildDistributionError(
            f"--out-dir {raw_out_dir} is not empty; pass an empty or nonexistent "
            "directory so a stale distribution cannot be mixed into this build."
        )
    raw_out_dir.mkdir(parents=True, exist_ok=True)
    return raw_out_dir.resolve()


def _copy_source_tree(source_root: Path, destination: Path) -> None:
    """Copy exactly the files a wheel build reads — `pyproject.toml`,
    `_build_backend.py`, `README.md`, `LICENSE`, and `src/` — into
    `destination`, so the wheel is built from an isolated tree instead of
    the tracked checkout.
    """
    destination.mkdir(parents=True)
    for entry_name in _SOURCE_TREE_COPY_ENTRIES:
        source_entry = source_root / entry_name
        if not source_entry.exists():
            continue
        destination_entry = destination / entry_name
        if source_entry.is_dir():
            shutil.copytree(source_entry, destination_entry, ignore=_ignore_build_cruft)
        else:
            shutil.copyfile(source_entry, destination_entry)


def _ignore_build_cruft(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in _SOURCE_TREE_IGNORED_DIR_NAMES}


def _stage_artifact_into_source_tree(
    artifact: RcloneArtifact, staging_root: Path, source_tree: Path
) -> None:
    """Stage and verify `artifact`'s executable, then copy the staged
    directory into `source_tree`'s package-data location.

    Reuses `prepare_rclone_artifact`'s staging functions directly rather
    than duplicating the download/verify/extract sequence.
    """
    target_staging_dir = prepare_rclone_artifact.staging_directory(staging_root, artifact)
    target_staging_dir.mkdir(parents=True, exist_ok=True)
    prepare_rclone_artifact.stage_executable(artifact, target_staging_dir)
    prepare_rclone_artifact.stage_license(target_staging_dir)

    assets_destination = source_tree / _ASSETS_RELATIVE_DIR / artifact.wheel_platform_tag
    assets_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(target_staging_dir, assets_destination)


def _require_uv_executable() -> str:
    uv_executable = shutil.which("uv")
    if uv_executable is None:
        raise BuildDistributionError("The 'uv' executable was not found on PATH.")
    return uv_executable


def _wheel_names(out_dir: Path) -> set[str]:
    return {path.name for path in out_dir.glob("*.whl")}


def _build_wheel(source_tree: Path, out_dir: Path) -> Path:
    uv_executable = _require_uv_executable()
    before = _wheel_names(out_dir)
    subprocess.run(
        [uv_executable, "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=source_tree,
        check=True,
    )
    new_wheels = sorted(_wheel_names(out_dir) - before)
    if len(new_wheels) != 1:
        raise BuildDistributionError(
            f"Expected exactly one new wheel in {out_dir}, found {new_wheels!r}."
        )
    return out_dir / new_wheels[0]


def _verify_wheel(wheel_path: Path) -> None:
    result = verify_distribution.verify_wheel(wheel_path)
    if not result.passed:
        joined_violations = "\n".join(f"  - {violation}" for violation in result.violations)
        raise BuildDistributionError(f"{wheel_path.name} failed verification:\n{joined_violations}")


def _python_version() -> str:
    return _PYTHON_VERSION_FILE.read_text(encoding="utf-8").strip()


def _smoke_env_python_and_scripts_dir(smoke_env: Path) -> tuple[Path, Path]:
    if sys.platform == "win32":
        return smoke_env / "Scripts" / "python.exe", smoke_env / "Scripts"
    return smoke_env / "bin" / "python", smoke_env / "bin"


def _poisoned_proxy_env() -> dict[str, str]:
    """Return a copy of the current environment with the proxy variables
    poisoned to an unreachable address, as best-effort network isolation for
    the smoke test.
    """
    env = os.environ.copy()
    env[_HTTP_PROXY_ENV_VAR] = _HTTP_PROXY_POISON_URL
    env[_HTTPS_PROXY_ENV_VAR] = _HTTP_PROXY_POISON_URL
    env[_NO_PROXY_ENV_VAR] = ""
    return env


def _smoke_test_wheel(wheel_path: Path, smoke_env: Path) -> None:
    uv_executable = _require_uv_executable()
    subprocess.run(
        [uv_executable, "venv", "--python", _python_version(), str(smoke_env)], check=True
    )
    python_executable, scripts_dir = _smoke_env_python_and_scripts_dir(smoke_env)
    subprocess.run(
        [uv_executable, "pip", "install", "--python", str(python_executable), str(wheel_path)],
        check=True,
    )
    subprocess.run(
        [str(python_executable), str(_SMOKE_TEST_SCRIPT), str(scripts_dir)],
        check=True,
        env=_poisoned_proxy_env(),
    )


def _build_and_verify_wheel(artifact: RcloneArtifact, out_dir: Path) -> BuiltWheel:
    with TemporaryDirectory(prefix=_BUILD_ROOT_TEMP_PREFIX) as raw_build_root:
        build_root = Path(raw_build_root)
        source_tree = build_root / _SOURCE_TREE_DIRECTORY_NAME
        staging_root = build_root / _STAGING_SUBDIRECTORY_NAME

        _copy_source_tree(_REPO_ROOT, source_tree)
        _stage_artifact_into_source_tree(artifact, staging_root, source_tree)
        wheel_path = _build_wheel(source_tree, out_dir)
        _verify_wheel(wheel_path)
        _smoke_test_wheel(wheel_path, build_root / _SMOKE_ENV_DIRECTORY_NAME)

    return BuiltWheel(wheel_path, sha256_of_file(wheel_path))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        required=True,
        choices=_target_choices(),
        help="Certified build target to produce a wheel for.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help=(
            "Directory to place the verified wheel in. Must be empty or "
            "nonexistent; created if missing. Defaults to a fresh unique "
            "temporary directory."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Build, stage, verify, and smoke-test exactly one certified wheel.

    Returns 0 when the wheel is built, passes every
    `scripts/verify_distribution.py` check, installs cleanly, and passes the
    smoke test. Returns 1 and prints a diagnostic to stderr on any failure;
    no partial or unverified wheel is left in a caller-supplied `--out-dir`
    in that case, since `uv build` only writes a wheel on success.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        artifact = _resolve_target_artifact(args.target)
        _require_running_on_target_platform(artifact)
        out_dir = _prepare_output_directory(args.out_dir)
        built_wheel = _build_and_verify_wheel(artifact, out_dir)
    except (RcloneRuntimeError, BuildDistributionError, subprocess.CalledProcessError) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        return 1
    print(f"Verified wheel: {built_wheel.path}")
    print(f"SHA-256: {built_wheel.sha256_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
