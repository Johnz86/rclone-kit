"""Canonical build orchestration for one native (C ABI) rclone-kit bundle.

Builds the diagnostic `rclone` executable and the `librclone_kit` shared
library from the pinned `native/rclone` submodule, using the toolchain
recorded in `native/toolchain.toml`, then generates a manifest, `SHA256SUMS`,
and a native smoke-test result — see
`reference/rclone_c_abi_implementation_plan.md`'s "Native build process"
section, which this script implements. This is the single command a
developer or CI job runs; no other script or manual shell sequence is the
source of truth for a native build.

The `development` profile builds with no mount support (`-tags cmount` is not
passed). The `production` profile additionally passes `-tags cmount` and
requires the WinFsp SDK (headers only, at build time) to be installed on the
Windows build host - it does not need to be installed on end-user machines
for anything except actually calling `mount()`/`mount_s3()`, which also needs
the separate WinFsp runtime/driver installed and running (see
`native_c_abi_wave_h_review_and_design.md`'s mount addendum for how this was
verified against a real mount). Only `windows-amd64`, built on a Windows
amd64 host, is implemented; the Linux target must be built inside a pinned
manylinux container that does not exist yet.

Usage:
    uv run python scripts/native/build.py --target windows-amd64
    uv run python scripts/native/build.py --target windows-amd64 --out-dir build/native/windows-amd64
"""

import argparse
import json
import os
import platform as _platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import manifest as native_manifest
import smoke as native_smoke

from rclone_kit.runtime.exceptions import UnsupportedPlatformError
from rclone_kit.runtime.native_platform import (
    NativeTarget,
    native_target_choices,
    resolve_native_target,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_NATIVE_ROOT = _REPO_ROOT / "native"
_SUBMODULE_DIR = _NATIVE_ROOT / "rclone"
_TOOLCHAIN_MANIFEST_PATH = _NATIVE_ROOT / "toolchain.toml"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"
_RCLONE_LICENSE_SOURCE = _REPO_ROOT / "licenses" / "rclone" / "COPYING"

_ABI_HEADER_SOURCE = _SUBMODULE_DIR / "librclone" / "rclonekit" / "abi.h"
_ABI_HEADER_OUTPUT_NAME = "rclonekit_abi.h"
_LICENSE_OUTPUT_NAME = "RCLONE_LICENSE"
_MANIFEST_OUTPUT_NAME = "native-manifest.json"
_SHA256SUMS_OUTPUT_NAME = "SHA256SUMS"
_SMOKE_RESULTS_OUTPUT_NAME = "smoke-results.json"

_FOCUSED_GO_TEST_PACKAGES = ("./lib/oauthutil", "./fs/config", "./librclone/...")
_BRIDGE_PACKAGE = "./librclone/rclonekit"

_DEVELOPMENT_PROFILE = "development"
_PRODUCTION_PROFILE = "production"
_SUPPORTED_PROFILES = (_DEVELOPMENT_PROFILE, _PRODUCTION_PROFILE)
_MOUNT_BUILD_TAG = "cmount"

# Both Program Files locations are checked because WinFsp's own installer
# offers either, matching upstream rclone's own Windows CI convention
# (native/rclone/.github/workflows/build.yml sets CPATH to both).
_WINFSP_FUSE_INCLUDE_CANDIDATES = (
    Path(r"C:\Program Files\WinFsp\inc\fuse"),
    Path(r"C:\Program Files (x86)\WinFsp\inc\fuse"),
)


class NativeBuildError(Exception):
    """Raised for an orchestration failure that is not already a documented
    error raised by a composed step.
    """


def _toolchain_manifest() -> dict:
    if not _TOOLCHAIN_MANIFEST_PATH.is_file():
        raise NativeBuildError(f"Missing {_TOOLCHAIN_MANIFEST_PATH}.")
    with _TOOLCHAIN_MANIFEST_PATH.open("rb") as handle:
        return tomllib.load(handle)


def _rclone_kit_version() -> str:
    with _PYPROJECT_PATH.open("rb") as handle:
        pyproject = tomllib.load(handle)
    return pyproject["project"]["version"]


def _require_go_executable() -> str:
    go_executable = shutil.which("go")
    if go_executable is None:
        raise NativeBuildError("The 'go' executable was not found on PATH.")
    return go_executable


def _require_go_version_matches(go_executable: str, toolchain: dict) -> None:
    expected = f"go{toolchain['go_version']}"
    result = subprocess.run([go_executable, "version"], capture_output=True, text=True, check=True)
    if expected not in result.stdout.split():
        raise NativeBuildError(
            f"Resolved Go toolchain {result.stdout.strip()!r} does not match "
            f"native/toolchain.toml's go_version={toolchain['go_version']!r}."
        )


def _windows_cc(toolchain: dict) -> str:
    cc_path = toolchain["windows_compiler_cc"]
    if not Path(cc_path).is_file():
        raise NativeBuildError(
            f"native/toolchain.toml's windows_compiler_cc {cc_path!r} does not exist. "
            "See native/README.md's Windows compiler note."
        )
    return cc_path


def _require_running_on_target_platform(target: NativeTarget) -> None:
    """Fail fast when `target` does not match the running host.

    This script does not cross-compile: the Windows CC path is a concrete
    installed toolchain, and no pinned Linux (manylinux) build environment is
    wired up yet.
    """
    try:
        running_target = resolve_native_target(
            system=_platform.system(), machine=_platform.machine()
        )
    except UnsupportedPlatformError as error:
        raise NativeBuildError(str(error)) from error
    if running_target.wheel_platform_tag != target.wheel_platform_tag:
        raise NativeBuildError(
            f"Requested target {target.wheel_platform_tag!r} does not match the "
            f"running platform {running_target.wheel_platform_tag!r}; this script "
            "does not cross-compile."
        )


def _require_submodule_present() -> None:
    if not (_SUBMODULE_DIR / ".git").exists():
        raise NativeBuildError(
            f"{_SUBMODULE_DIR} is not an initialized Git submodule. "
            "Run 'git submodule update --init native/rclone'."
        )


def _require_winfsp_fuse_include_dir() -> Path:
    """Locate the installed WinFsp SDK's `inc/fuse` directory (headers
    only - no `.lib`/LDFLAGS linking is needed; cgofuse's Windows support
    loads the WinFsp runtime DLL dynamically, confirmed empirically by
    building and running a real mount, not assumed).
    """
    for candidate in _WINFSP_FUSE_INCLUDE_CANDIDATES:
        if candidate.is_dir():
            return candidate
    raise NativeBuildError(
        "No installed WinFsp SDK found (checked: "
        f"{', '.join(str(c) for c in _WINFSP_FUSE_INCLUDE_CANDIDATES)}). "
        "The --profile production build needs the WinFsp SDK's headers at build "
        "time; install WinFsp (including its developer/SDK component) from "
        "https://winfsp.dev/ first."
    )


def _build_env(cc_path: str, *, mount_tags: bool) -> dict[str, str]:
    env = os.environ.copy()
    env["CGO_ENABLED"] = "1"
    env["CC"] = cc_path
    if mount_tags:
        fuse_include_dir = _require_winfsp_fuse_include_dir()
        # CPATH is a plain gcc/clang search-path env var (not Go-specific):
        # matches upstream rclone's own Windows CI convention for the same
        # cmount build (native/rclone/.github/workflows/build.yml).
        existing_cpath = env.get("CPATH", "")
        env["CPATH"] = (
            f"{fuse_include_dir}{os.pathsep}{existing_cpath}"
            if existing_cpath
            else str(fuse_include_dir)
        )
    return env


def _run_focused_go_tests(go_executable: str, env: dict[str, str]) -> None:
    subprocess.run(
        [go_executable, "test", *_FOCUSED_GO_TEST_PACKAGES],
        cwd=_SUBMODULE_DIR,
        env=env,
        check=True,
    )


def _build_tags(mount_tags: bool) -> tuple[str, ...]:
    return (_MOUNT_BUILD_TAG,) if mount_tags else ()


def _tags_args(mount_tags: bool) -> list[str]:
    tags = _build_tags(mount_tags)
    return ["-tags", ",".join(tags)] if tags else []


def _build_executable(
    go_executable: str, env: dict[str, str], output_path: Path, *, mount_tags: bool
) -> None:
    subprocess.run(
        [
            go_executable,
            "build",
            "-trimpath",
            "-buildvcs=true",
            *_tags_args(mount_tags),
            "-o",
            str(output_path),
            ".",
        ],
        cwd=_SUBMODULE_DIR,
        env=env,
        check=True,
    )


def _build_library(
    go_executable: str, env: dict[str, str], output_path: Path, *, mount_tags: bool
) -> None:
    subprocess.run(
        [
            go_executable,
            "build",
            "-trimpath",
            "-buildvcs=true",
            "-buildmode=c-shared",
            *_tags_args(mount_tags),
            "-o",
            str(output_path),
            _BRIDGE_PACKAGE,
        ],
        cwd=_SUBMODULE_DIR,
        env=env,
        check=True,
    )


def build_native_target(
    target: NativeTarget, out_dir: Path, *, profile: str = _DEVELOPMENT_PROFILE
) -> Path:
    """Build, test, stage, manifest, hash, and smoke-test one native target.

    Returns `out_dir`. Raises `NativeBuildError` or a propagated
    `subprocess.CalledProcessError`/`manifest.NativeManifestError`/
    `smoke.NativeSmokeTestError` on any failure; no partial manifest or
    `SHA256SUMS` is written in that case (they are the last steps).
    """
    if profile not in _SUPPORTED_PROFILES:
        raise NativeBuildError(
            f"Unsupported --profile {profile!r}; expected one of {_SUPPORTED_PROFILES!r}."
        )
    mount_tags = profile == _PRODUCTION_PROFILE
    _require_submodule_present()
    _require_running_on_target_platform(target)

    toolchain = _toolchain_manifest()
    go_executable = _require_go_executable()
    _require_go_version_matches(go_executable, toolchain)
    cc_path = _windows_cc(toolchain)
    env = _build_env(cc_path, mount_tags=mount_tags)

    out_dir.mkdir(parents=True, exist_ok=True)
    executable_path = out_dir / target.executable_filename
    library_path = out_dir / target.library_filename

    _run_focused_go_tests(go_executable, env)
    _build_executable(go_executable, env, executable_path, mount_tags=mount_tags)
    _build_library(go_executable, env, library_path, mount_tags=mount_tags)

    shutil.copyfile(_ABI_HEADER_SOURCE, out_dir / _ABI_HEADER_OUTPUT_NAME)
    shutil.copyfile(_RCLONE_LICENSE_SOURCE, out_dir / _LICENSE_OUTPUT_NAME)

    smoke_result = native_smoke.run_smoke_test(library_path)
    (out_dir / _SMOKE_RESULTS_OUTPUT_NAME).write_text(
        json.dumps(smoke_result, indent=2) + "\n", encoding="utf-8"
    )

    fork = native_manifest.fork_provenance(_SUBMODULE_DIR, toolchain["rclone_fork_url"])
    toolchain_info = native_manifest.toolchain_provenance(go_executable, cc_path)
    outputs = native_manifest.output_files(
        out_dir,
        [
            target.executable_filename,
            target.library_filename,
            _ABI_HEADER_OUTPUT_NAME,
            _LICENSE_OUTPUT_NAME,
        ],
    )
    built_manifest = native_manifest.build_manifest(
        rclone_kit_version=_rclone_kit_version(),
        c_abi_version=toolchain["c_abi_version"],
        rclone_upstream_version=toolchain["rclone_upstream_version"],
        fork=fork,
        toolchain=toolchain_info,
        target=target,
        outputs=outputs,
        go_build_tags=_build_tags(mount_tags),
    )
    native_manifest.write_manifest(built_manifest, out_dir / _MANIFEST_OUTPUT_NAME)
    native_manifest.write_sha256sums(outputs, out_dir / _SHA256SUMS_OUTPUT_NAME)

    return out_dir


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", required=True, choices=native_target_choices(), help="Native build target."
    )
    parser.add_argument(
        "--profile",
        default=_DEVELOPMENT_PROFILE,
        choices=_SUPPORTED_PROFILES,
        help=(
            "Build profile: 'development' (no mount support) or 'production' "
            "(adds mount()/mount_s3() support via -tags cmount; needs the WinFsp SDK "
            "installed at build time)."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory. Defaults to build/native/<target>/.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Build one native target end to end.

    Returns 0 when the build, focused Go tests, manifest generation, and
    native smoke test all pass. Returns 1 and prints a diagnostic to stderr
    on any failure.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    system, _separator, machine = args.target.partition("-")
    try:
        target = resolve_native_target(system=system, machine=machine)
        out_dir = args.out_dir or (_REPO_ROOT / "build" / "native" / args.target)
        result_dir = build_native_target(target, out_dir, profile=args.profile)
    except (
        NativeBuildError,
        native_manifest.NativeManifestError,
        native_smoke.NativeSmokeTestError,
        subprocess.CalledProcessError,
        UnsupportedPlatformError,
    ) as error:
        print(f"Native build failed: {error}", file=sys.stderr)
        return 1
    print(f"Native build complete: {result_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
