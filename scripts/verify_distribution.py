"""Distribution verification for built `rclone-kit` wheels and sdists.

Supersedes the inline PowerShell/Python inspection step in
`.github/workflows/ci.yml`'s `package` job with a single, generically
testable check: given a directory containing one or more built `.whl` and
`.tar.gz` files, verify every release-readiness property a distribution
must satisfy before publishing.

Usage:
    uv run python scripts/verify_distribution.py dist

Each `check_*` function inspects one property of one distribution file and
returns a list of human-readable violation messages (an empty list means the
check passed). `main` runs every applicable check against every wheel and
sdist found in the given directory, prints a pass/fail summary, and exits
with status 1 if any check failed.
"""

import argparse
import configparser
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import Tag
from packaging.utils import parse_wheel_filename

from rclone_kit.native.runtime import EXPECTED_ABI_VERSION
from rclone_kit.runtime.exceptions import ArchiveMemberUnsafeError, UnsupportedPlatformError
from rclone_kit.runtime.native_platform import (
    SUPPORTED_NATIVE_TARGETS,
    NativeTarget,
    resolve_native_target,
)
from rclone_kit.runtime.platform import OperatingSystem

_WHEEL_GLOB_PATTERN = "*.whl"
_SDIST_GLOB_PATTERN = "*.tar.gz"

_PURE_PYTHON_PLATFORM_TAG = "any"
_EXPECTED_WHEEL_PYTHON_TAG = "py3"
_EXPECTED_WHEEL_ABI_TAG = "none"

_PACKAGED_PACKAGE_NAME = "rclone_kit"
_PACKAGED_ASSETS_RELATIVE_DIR = "assets/native"
_NATIVE_MANIFEST_FILENAME = "native-manifest.json"
_SHA256SUMS_FILENAME = "SHA256SUMS"
_ABI_HEADER_FILENAME = "rclonekit_abi.h"
_RCLONE_LICENSE_FILENAME = "RCLONE_LICENSE"
_MOUNT_BUILD_TAG = "cmount"
_RCLONE_KIT_LICENSE_MEMBER_SUFFIX = ".dist-info/licenses/LICENSE"
_METADATA_MEMBER_SUFFIX = ".dist-info/METADATA"
_ENTRY_POINTS_MEMBER_SUFFIX = ".dist-info/entry_points.txt"
_CONSOLE_SCRIPTS_SECTION = "console_scripts"

_REQUIRES_PYTHON_FIELD = "Requires-Python"
_REQUIRES_DIST_FIELD_PREFIX = "Requires-Dist: "

_MINIMUM_PYTHON_VERSION = "3.13"
_PROBE_PYTHON_VERSIONS_BELOW_FLOOR: tuple[str, ...] = (
    "2.7",
    "3.6",
    "3.9",
    "3.10",
    "3.11",
    "3.12",
    "3.12.99",
)

_DEV_ONLY_DISTRIBUTION_NAMES: frozenset[str] = frozenset(
    {
        "ruff",
        "pytest",
        "pytest-xdist",
        "pyright",
        "build",
        "twine",
        "black",
        "isort",
        "flake8",
        "pylint",
        "mypy",
        "tox",
        "setuptools-scm",
    }
)

_DENYLISTED_PATH_SEGMENTS: frozenset[str] = frozenset(
    {
        "__pycache__",
        "tests",
        "test",
        ".git",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
    }
)
_DENYLISTED_FILENAME_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo", ".log")
_DENYLISTED_EXACT_FILENAMES: frozenset[str] = frozenset(
    {".env", ".ds_store", "thumbs.db", "conftest.py", ".coverage"}
)

_ENTRY_POINT_PROBE_SCRIPT = """
import importlib
import sys

extract_dir, module_name, attribute_path = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, extract_dir)
module = importlib.import_module(module_name)
target = module
for part in attribute_path.split("."):
    target = getattr(target, part)
if not callable(target):
    raise SystemExit(f"resolved target is not callable: {module_name}:{attribute_path}")
"""


@dataclass(frozen=True)
class DistributionVerificationResult:
    """The outcome of running every applicable check against one built
    distribution file.

    `violations` is empty exactly when every check passed.
    """

    file_name: str
    violations: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """Return whether every check against this distribution file passed."""
        return len(self.violations) == 0


def discover_wheels(dist_dir: Path) -> tuple[Path, ...]:
    """Return every `.whl` file directly inside `dist_dir`, sorted by name."""
    return tuple(sorted(dist_dir.glob(_WHEEL_GLOB_PATTERN)))


def discover_sdists(dist_dir: Path) -> tuple[Path, ...]:
    """Return every `.tar.gz` file directly inside `dist_dir`, sorted by name."""
    return tuple(sorted(dist_dir.glob(_SDIST_GLOB_PATTERN)))


def _list_zip_members(archive_path: Path) -> tuple[str, ...]:
    with zipfile.ZipFile(archive_path) as archive:
        return tuple(archive.namelist())


def _read_zip_member(archive_path: Path, member_name: str) -> bytes:
    with zipfile.ZipFile(archive_path) as archive:
        return archive.read(member_name)


def _list_tar_members(archive_path: Path) -> tuple[str, ...]:
    with tarfile.open(archive_path) as archive:
        return tuple(archive.getnames())


def _find_member_by_suffix(members: tuple[str, ...], suffix: str) -> str | None:
    return next((member for member in members if member.endswith(suffix)), None)


def _reject_unsafe_member_path(member_filename: str) -> None:
    pure_path = PurePosixPath(member_filename)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        raise ArchiveMemberUnsafeError(member_filename)


def _safe_extract_zip(archive_path: Path, destination: Path) -> None:
    """Extract every member of the zip at `archive_path` into `destination`.

    Validates each member's recorded path before writing anything, applying
    the same path-traversal invariant as
    `rclone_kit.runtime.archive_extract.extract_single_member`. Raises
    `ArchiveMemberUnsafeError` when a member's path is absolute or escapes
    `destination` through a parent-directory segment.
    """
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            _reject_unsafe_member_path(member.filename)
        for member in archive.infolist():
            target_path = destination / member.filename
            if member.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target_path.open("wb") as target:
                shutil.copyfileobj(source, target)


_WHEEL_TAG_OS_PREFIX_TO_SYSTEM: dict[str, str] = {
    "win": "windows",
    "linux": "linux",
    "manylinux": "linux",
}
_WHEEL_TAG_ARCH_SUFFIX_TO_MACHINE: dict[str, str] = {
    "amd64": "amd64",
    "x86_64": "amd64",
}


def _wheel_tags(wheel_path: Path) -> tuple[Tag, ...]:
    _name, _version, _build, tags = parse_wheel_filename(wheel_path.name)
    return tuple(tags)


def _wheel_platform_tags(wheel_path: Path) -> tuple[str, ...]:
    return tuple(tag.platform for tag in _wheel_tags(wheel_path))


def _expected_native_target_for_wheel(wheel_path: Path) -> NativeTarget | None:
    """Resolve the certified `NativeTarget` a wheel's own platform tag
    implies, using `rclone_kit.runtime.native_platform`'s existing platform
    model instead of a second hardcoded platform table.

    Returns `None` when the wheel's platform tag does not map to any
    certified build target (including a pure-Python tag).
    """
    platform_tags = _wheel_platform_tags(wheel_path)
    if len(platform_tags) != 1:
        return None
    lowered_tag = platform_tags[0].lower()
    system = next(
        (
            system
            for prefix, system in _WHEEL_TAG_OS_PREFIX_TO_SYSTEM.items()
            if lowered_tag.startswith(prefix)
        ),
        None,
    )
    machine = next(
        (
            machine
            for suffix, machine in _WHEEL_TAG_ARCH_SUFFIX_TO_MACHINE.items()
            if lowered_tag.endswith(suffix)
        ),
        None,
    )
    if system is None or machine is None:
        return None
    try:
        return resolve_native_target(system=system, machine=machine)
    except UnsupportedPlatformError:
        return None


def _asset_member_path(native_target: NativeTarget, filename: str) -> str:
    """Return the wheel-internal member path for a file staged under
    `<package>/assets/native/<wheel_platform_tag>/` for `native_target`.

    Mirrors the staging convention written by
    `scripts/build_distribution.py` and read at runtime by
    `rclone_kit.native.library.default_packaged_assets_root`.
    """
    return (
        f"{_PACKAGED_PACKAGE_NAME}/{_PACKAGED_ASSETS_RELATIVE_DIR}/"
        f"{native_target.wheel_platform_tag}/{filename}"
    )


def check_platform_independent_tag(wheel_path: Path) -> list[str]:
    """Fail when `wheel_path` is tagged platform-independent (`py3-none-any`).

    `rclone-kit` wheels always bundle a platform-specific native library, so
    a pure-Python tag means the build backend's platform-forcing shim
    (`_build_backend.py`) did not take effect.
    """
    return [
        f"{wheel_path.name}: tagged platform-independent ({tag!r}); "
        "rclone-kit wheels must declare a concrete platform."
        for tag in _wheel_platform_tags(wheel_path)
        if tag.lower() == _PURE_PYTHON_PLATFORM_TAG
    ]


def check_exact_wheel_tag(wheel_path: Path) -> list[str]:
    """Fail unless `wheel_path`'s full tag is exactly `(py3, none,
    <native_target.wheel_platform_tag>)` for the certified target its
    platform component implies.

    `_build_backend.py` forces a platform-specific wheel with no CPython ABI
    dependency; a concrete interpreter/ABI tag such as `cp313-cp313` means
    its `bdist_wheel.get_tag` override did not take effect.
    """
    native_target = _expected_native_target_for_wheel(wheel_path)
    if native_target is None:
        return []
    tags = _wheel_tags(wheel_path)
    if len(tags) != 1:
        return []
    tag = tags[0]
    actual = (tag.interpreter, tag.abi, tag.platform.lower())
    expected = (
        _EXPECTED_WHEEL_PYTHON_TAG,
        _EXPECTED_WHEEL_ABI_TAG,
        native_target.wheel_platform_tag,
    )
    if actual != expected:
        return [
            f"{wheel_path.name}: wheel tag {actual!r} does not match the expected "
            f"ABI-independent tag {expected!r}."
        ]
    return []


def check_bundled_library_present(wheel_path: Path, members: tuple[str, ...]) -> list[str]:
    """Fail when the native library implied by `wheel_path`'s platform tag,
    or any of its required companion files, are absent from `members`.
    """
    native_target = _expected_native_target_for_wheel(wheel_path)
    if native_target is None:
        return [
            f"{wheel_path.name}: platform tag does not map to a certified native "
            "build target; cannot verify a bundled library is present."
        ]
    required_filenames = (
        native_target.library_filename,
        _NATIVE_MANIFEST_FILENAME,
        _SHA256SUMS_FILENAME,
        _RCLONE_LICENSE_FILENAME,
        _ABI_HEADER_FILENAME,
    )
    violations: list[str] = []
    for filename in required_filenames:
        expected_member = _asset_member_path(native_target, filename)
        if expected_member not in members:
            violations.append(
                f"{wheel_path.name}: missing expected bundled file {expected_member!r} "
                f"for platform {native_target.wheel_platform_tag!r}."
            )
    return violations


def check_no_foreign_platform_library(wheel_path: Path, members: tuple[str, ...]) -> list[str]:
    """Fail when `wheel_path` bundles a native library staged for a
    different certified platform than the one its own tag implies.
    """
    native_target = _expected_native_target_for_wheel(wheel_path)
    if native_target is None:
        return []
    violations: list[str] = []
    for other_target in SUPPORTED_NATIVE_TARGETS:
        if other_target.wheel_platform_tag == native_target.wheel_platform_tag:
            continue
        foreign_member = _asset_member_path(other_target, other_target.library_filename)
        if foreign_member in members:
            violations.append(
                f"{wheel_path.name}: a {native_target.wheel_platform_tag} wheel unexpectedly "
                f"bundles another platform's library {foreign_member!r}."
            )
    return violations


def _read_native_manifest(wheel_path: Path, native_target: NativeTarget, members: tuple[str, ...]):
    manifest_member = _asset_member_path(native_target, _NATIVE_MANIFEST_FILENAME)
    if manifest_member not in members:
        return None
    return json.loads(_read_zip_member(wheel_path, manifest_member).decode("utf-8"))


def check_bundled_library_hash(wheel_path: Path, members: tuple[str, ...]) -> list[str]:
    """Fail when the bundled library's SHA-256 disagrees with its own
    shipped `native-manifest.json`'s recorded digest for that filename.

    Catches a corrupted library staged alongside a manifest generated for a
    different (valid) build.
    """
    native_target = _expected_native_target_for_wheel(wheel_path)
    if native_target is None:
        return []
    library_member = _asset_member_path(native_target, native_target.library_filename)
    manifest = _read_native_manifest(wheel_path, native_target, members)
    if library_member not in members or manifest is None:
        return []
    manifest_digest = next(
        (
            output.get("sha256_digest")
            for output in manifest.get("outputs", [])
            if output.get("filename") == native_target.library_filename
        ),
        None,
    )
    if manifest_digest is None:
        return [
            f"{wheel_path.name}: {_NATIVE_MANIFEST_FILENAME} has no digest recorded for "
            f"{native_target.library_filename!r}."
        ]
    actual_digest = hashlib.sha256(_read_zip_member(wheel_path, library_member)).hexdigest()
    if actual_digest != manifest_digest:
        return [
            f"{wheel_path.name}: bundled library SHA-256 {actual_digest} does not match "
            f"its shipped manifest ({manifest_digest})."
        ]
    return []


def check_bundled_library_has_mount_support(
    wheel_path: Path, members: tuple[str, ...]
) -> list[str]:
    """Fail unless the shipped native library was built with real mount
    support.

    `mount()`/`mount_s3()` are public API with no CLI fallback; a wheel built
    without mount support compiled in would silently regress that surface
    for every installer. What "compiled in" means is platform-specific:

    - Windows only has `cmd/cmount` (WinFsp/cgofuse), gated behind the
      `cmount` Go build tag - the manifest must record it.
    - Linux's `cmd/mount` (bazil.org/fuse) is imported unconditionally with
      no build tag of its own and is always tried first by
      `mountlib.ResolveMountMethod` - verified empirically inside a
      `manylinux2014_x86_64` container with no extra build-time package at
      all - so every Linux build already has mount support; there is
      nothing to check in the manifest for it.
    """
    native_target = _expected_native_target_for_wheel(wheel_path)
    if native_target is None:
        return []
    if native_target.operating_system != OperatingSystem.WINDOWS:
        return []
    manifest = _read_native_manifest(wheel_path, native_target, members)
    if manifest is None:
        return []
    if _MOUNT_BUILD_TAG not in manifest.get("go_build_tags", []):
        return [
            f"{wheel_path.name}: {_NATIVE_MANIFEST_FILENAME} does not record the "
            f"{_MOUNT_BUILD_TAG!r} build tag; this wheel's library was not built with "
            "--profile production and mount()/mount_s3() would not work."
        ]
    return []


def check_bundled_library_abi_version(wheel_path: Path, members: tuple[str, ...]) -> list[str]:
    """Fail unless the shipped `native-manifest.json`'s `c_abi_version`
    matches this Python binding's own `EXPECTED_ABI_VERSION`.

    Catches a Python/native ABI mismatch at build-verification time instead
    of at the first real `ctypes` call in production.
    """
    native_target = _expected_native_target_for_wheel(wheel_path)
    if native_target is None:
        return []
    manifest = _read_native_manifest(wheel_path, native_target, members)
    if manifest is None:
        return []
    actual_abi_version = manifest.get("c_abi_version")
    if actual_abi_version != EXPECTED_ABI_VERSION:
        return [
            f"{wheel_path.name}: {_NATIVE_MANIFEST_FILENAME}'s c_abi_version "
            f"{actual_abi_version!r} does not match this binding's expected "
            f"{EXPECTED_ABI_VERSION!r}."
        ]
    return []


def check_required_licenses_present(wheel_path: Path, members: tuple[str, ...]) -> list[str]:
    """Fail when either the rclone-kit project license or the bundled
    rclone MIT license is absent from `members`.
    """
    violations: list[str] = []
    if not any(member.endswith(_RCLONE_KIT_LICENSE_MEMBER_SUFFIX) for member in members):
        violations.append(
            f"{wheel_path.name}: missing rclone-kit project license "
            f"(no member ending in {_RCLONE_KIT_LICENSE_MEMBER_SUFFIX!r})."
        )
    native_target = _expected_native_target_for_wheel(wheel_path)
    if native_target is not None:
        rclone_license_member = _asset_member_path(native_target, _RCLONE_LICENSE_FILENAME)
        if rclone_license_member not in members:
            violations.append(
                f"{wheel_path.name}: missing rclone MIT license {rclone_license_member!r}."
            )
    return violations


def _parse_console_scripts(entry_points_text: str) -> dict[str, str]:
    parser = configparser.ConfigParser()
    parser.read_string(entry_points_text)
    if _CONSOLE_SCRIPTS_SECTION not in parser:
        return {}
    return dict(parser[_CONSOLE_SCRIPTS_SECTION])


def _verify_entry_point_target(
    wheel_name: str, script_name: str, target: str, extract_dir: Path
) -> list[str]:
    module_name, separator, attribute_path = target.partition(":")
    if not separator or not module_name or not attribute_path:
        return [
            f"{wheel_name}: console script {script_name!r} has a malformed "
            f"target {target!r}; expected 'module:callable'."
        ]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _ENTRY_POINT_PROBE_SCRIPT,
            str(extract_dir),
            module_name,
            attribute_path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr_lines = completed.stderr.strip().splitlines()
        detail = stderr_lines[-1] if stderr_lines else "unknown error"
        return [
            f"{wheel_name}: console script {script_name!r} target {target!r} "
            f"failed to resolve: {detail}"
        ]
    return []


def check_entry_points_resolve(wheel_path: Path, members: tuple[str, ...]) -> list[str]:
    """Fail when any `console_scripts` entry point targets a module that
    cannot be imported or a callable that cannot be resolved from it.

    Extracts the wheel to a temporary directory and imports each target in a
    fresh subprocess with that directory prepended to `sys.path`, rather than
    installing the wheel into a throwaway environment. A subprocess avoids
    both a slow, network-dependent venv-and-install cycle per wheel
    (`scripts/build_distribution.py` already performs a full
    install-and-smoke-test pass separately) and a `sys.modules` collision
    with the `rclone_kit` package this script itself imports for platform
    resolution; it still exercises the exact bytes shipped in the wheel and
    reuses this process's already-installed runtime dependencies.
    """
    entry_points_member = _find_member_by_suffix(members, _ENTRY_POINTS_MEMBER_SUFFIX)
    if entry_points_member is None:
        return [f"{wheel_path.name}: missing {_ENTRY_POINTS_MEMBER_SUFFIX!r}."]
    entry_points_text = _read_zip_member(wheel_path, entry_points_member).decode("utf-8")
    console_scripts = _parse_console_scripts(entry_points_text)
    if not console_scripts:
        return [
            f"{wheel_path.name}: no [{_CONSOLE_SCRIPTS_SECTION}] entries found in "
            f"{entry_points_member!r}."
        ]

    with TemporaryDirectory() as raw_extract_dir:
        extract_dir = Path(raw_extract_dir)
        _safe_extract_zip(wheel_path, extract_dir)
        return [
            violation
            for script_name, target in console_scripts.items()
            for violation in _verify_entry_point_target(
                wheel_path.name, script_name, target, extract_dir
            )
        ]


def _parse_metadata_field(metadata_text: str, field_name: str) -> str | None:
    prefix = f"{field_name}: "
    return next(
        (
            line[len(prefix) :].strip()
            for line in metadata_text.splitlines()
            if line.startswith(prefix)
        ),
        None,
    )


def _requires_python_excludes_versions_below_floor(specifier_text: str) -> bool:
    """Return whether `specifier_text` admits none of a fixed probe set of
    Python versions below `_MINIMUM_PYTHON_VERSION`.

    A specifier stricter than the floor (for example `>=3.14`) still passes:
    the plan's requirement is that no version below 3.13 is declared
    supported, not that 3.13 itself must be admitted.
    """
    specifier = SpecifierSet(specifier_text)
    return not any(
        specifier.contains(version, prereleases=True)
        for version in _PROBE_PYTHON_VERSIONS_BELOW_FLOOR
    )


def check_requires_python_floor(wheel_path: Path, members: tuple[str, ...]) -> list[str]:
    """Fail when `METADATA`'s `Requires-Python` specifier admits any Python
    version below 3.13, parsing it with `packaging.specifiers.SpecifierSet`
    rather than substring-matching an exact literal.
    """
    metadata_member = _find_member_by_suffix(members, _METADATA_MEMBER_SUFFIX)
    if metadata_member is None:
        return [f"{wheel_path.name}: missing {_METADATA_MEMBER_SUFFIX!r}."]
    metadata_text = _read_zip_member(wheel_path, metadata_member).decode("utf-8")
    requires_python = _parse_metadata_field(metadata_text, _REQUIRES_PYTHON_FIELD)
    if requires_python is None:
        return [f"{wheel_path.name}: METADATA has no {_REQUIRES_PYTHON_FIELD} field."]
    if not _requires_python_excludes_versions_below_floor(requires_python):
        return [
            f"{wheel_path.name}: {_REQUIRES_PYTHON_FIELD} {requires_python!r} does not "
            f"exclude every Python version below {_MINIMUM_PYTHON_VERSION}."
        ]
    return []


def check_no_dev_tools_in_requires_dist(wheel_path: Path, members: tuple[str, ...]) -> list[str]:
    """Fail when `METADATA` declares a development-only tool (Ruff, pytest,
    Pyright, and similar) as a runtime dependency.
    """
    metadata_member = _find_member_by_suffix(members, _METADATA_MEMBER_SUFFIX)
    if metadata_member is None:
        return []
    metadata_text = _read_zip_member(wheel_path, metadata_member).decode("utf-8")
    violations: list[str] = []
    for line in metadata_text.splitlines():
        if not line.startswith(_REQUIRES_DIST_FIELD_PREFIX):
            continue
        raw_requirement = line[len(_REQUIRES_DIST_FIELD_PREFIX) :].strip()
        requirement_name = Requirement(raw_requirement).name.lower()
        if requirement_name in _DEV_ONLY_DISTRIBUTION_NAMES:
            violations.append(
                f"{wheel_path.name}: runtime dependency {raw_requirement!r} is a "
                "development-only tool and must live in [dependency-groups] instead."
            )
    return violations


def _is_denylisted_member(member_name: str) -> bool:
    pure_path = PurePosixPath(member_name)
    lowered_segments = {part.lower() for part in pure_path.parts}
    if lowered_segments & _DENYLISTED_PATH_SEGMENTS:
        return True
    lowered_filename = pure_path.name.lower()
    if lowered_filename in _DENYLISTED_EXACT_FILENAMES:
        return True
    return lowered_filename.endswith(_DENYLISTED_FILENAME_SUFFIXES)


def check_wheel_denylisted_members(wheel_path: Path, members: tuple[str, ...]) -> list[str]:
    """Fail when `wheel_path` contains a cache, test, secret, editor/OS
    cruft, or other unrelated build artifact matching a named denylist
    pattern.
    """
    return [
        f"{wheel_path.name}: contains denylisted path {member!r}."
        for member in members
        if _is_denylisted_member(member)
    ]


def check_sdist_denylisted_members(sdist_path: Path, members: tuple[str, ...]) -> list[str]:
    """Fail when `sdist_path` contains a cache, secret, editor/OS cruft, or
    other unrelated build artifact matching a named denylist pattern.

    Unlike a wheel, an sdist legitimately contains `tests/`-named source, so
    this reuses the shared denylist without the `tests`/`test` directory
    segments; those are still covered for wheels by
    `check_wheel_denylisted_members`.
    """
    return [
        f"{sdist_path.name}: contains denylisted path {member!r}."
        for member in members
        if _is_denylisted_member(member)
        and not any(segment in {"tests", "test"} for segment in PurePosixPath(member).parts)
    ]


def check_sdist_has_no_staged_native_libraries(
    sdist_path: Path, members: tuple[str, ...]
) -> list[str]:
    """Fail when `sdist_path` bundles a build-time-staged native library.

    A source distribution must stay platform-independent: platform-specific
    libraries are staged into `src/rclone_kit/assets/native/` only as
    ephemeral, gitignored build state (see `scripts/build_distribution.py`
    and `.github/workflows/ci.yml`) and must never ship inside an sdist,
    which `pip` may build into a wheel for any target platform.
    """
    violations: list[str] = []
    for native_target in SUPPORTED_NATIVE_TARGETS:
        suffix = (
            f"src/{_PACKAGED_PACKAGE_NAME}/{_PACKAGED_ASSETS_RELATIVE_DIR}/"
            f"{native_target.wheel_platform_tag}/{native_target.library_filename}"
        )
        violations.extend(
            f"{sdist_path.name}: source distribution must stay platform-independent "
            f"but bundles a staged library at {member!r}."
            for member in members
            if member.endswith(suffix)
        )
    return violations


def verify_wheel(wheel_path: Path) -> DistributionVerificationResult:
    """Run every wheel check against `wheel_path` and collect the results."""
    members = _list_zip_members(wheel_path)
    violations: list[str] = [
        *check_platform_independent_tag(wheel_path),
        *check_exact_wheel_tag(wheel_path),
        *check_bundled_library_present(wheel_path, members),
        *check_no_foreign_platform_library(wheel_path, members),
        *check_bundled_library_hash(wheel_path, members),
        *check_bundled_library_has_mount_support(wheel_path, members),
        *check_bundled_library_abi_version(wheel_path, members),
        *check_required_licenses_present(wheel_path, members),
        *check_entry_points_resolve(wheel_path, members),
        *check_requires_python_floor(wheel_path, members),
        *check_no_dev_tools_in_requires_dist(wheel_path, members),
        *check_wheel_denylisted_members(wheel_path, members),
    ]
    return DistributionVerificationResult(wheel_path.name, tuple(violations))


def verify_sdist(sdist_path: Path) -> DistributionVerificationResult:
    """Run every sdist check against `sdist_path` and collect the results."""
    members = _list_tar_members(sdist_path)
    violations: list[str] = [
        *check_sdist_denylisted_members(sdist_path, members),
        *check_sdist_has_no_staged_native_libraries(sdist_path, members),
    ]
    return DistributionVerificationResult(sdist_path.name, tuple(violations))


def check_release_set(dist_dir: Path) -> list[str]:
    """Fail unless `dist_dir` contains exactly one wheel per certified
    `SUPPORTED_NATIVE_TARGETS` target, with no duplicate or unrecognized
    wheel.

    Used only by CI's `release-assembly` job after every per-platform wheel
    job's artifact has been downloaded into one directory; a single-wheel
    build (one `wheel-*` CI job, or a local `build_distribution.py` run) has
    no use for this check.
    """
    wheels_by_tag: dict[str, list[Path]] = {
        native_target.wheel_platform_tag: [] for native_target in SUPPORTED_NATIVE_TARGETS
    }
    violations: list[str] = []
    for wheel_path in discover_wheels(dist_dir):
        native_target = _expected_native_target_for_wheel(wheel_path)
        if native_target is None:
            violations.append(f"{wheel_path.name}: not a recognized certified-target wheel.")
            continue
        wheels_by_tag[native_target.wheel_platform_tag].append(wheel_path)
    for wheel_platform_tag, matching_paths in wheels_by_tag.items():
        if not matching_paths:
            violations.append(f"Missing a wheel for certified target {wheel_platform_tag!r}.")
        elif len(matching_paths) > 1:
            names = ", ".join(path.name for path in matching_paths)
            violations.append(
                f"Multiple wheels found for certified target {wheel_platform_tag!r}: {names}."
            )
    return violations


def _report(results: list[DistributionVerificationResult]) -> int:
    failed = [result for result in results if not result.passed]
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.file_name}")
        for violation in result.violations:
            print(f"    - {violation}")
    print(f"\n{len(results) - len(failed)}/{len(results)} distribution files passed.")
    return 1 if failed else 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dist_dir",
        type=Path,
        help="Directory containing built .whl and .tar.gz files (e.g. 'dist').",
    )
    parser.add_argument(
        "--require-complete-release-set",
        action="store_true",
        help=(
            "Additionally require dist_dir to contain exactly one wheel per "
            "certified SUPPORTED_NATIVE_TARGETS target, with no duplicates. Only "
            "meaningful once every per-platform wheel has been collected "
            "into one directory (see CI's release-assembly job)."
        ),
    )
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def main(argv: list[str] | None = None) -> int:
    """Verify every wheel and sdist in the given directory.

    Returns 0 when every distribution file passes every applicable check
    (and, with `--require-complete-release-set`, the directory holds exactly
    one wheel per certified target). Returns 1 and prints a violation
    summary when any check fails, or when the directory contains no wheel
    or sdist files at all.
    """
    args = _parse_args(argv)
    dist_dir: Path = args.dist_dir
    if not dist_dir.is_dir():
        print(f"Not a directory: {dist_dir}", file=sys.stderr)
        return 1

    wheels = discover_wheels(dist_dir)
    sdists = discover_sdists(dist_dir)
    if not wheels and not sdists:
        print(f"No .whl or .tar.gz files found in {dist_dir}", file=sys.stderr)
        return 1

    results = [verify_wheel(wheel_path) for wheel_path in wheels]
    results += [verify_sdist(sdist_path) for sdist_path in sdists]
    exit_code = _report(results)

    if args.require_complete_release_set:
        release_set_violations = check_release_set(dist_dir)
        if release_set_violations:
            print("\n[FAIL] release set")
            for violation in release_set_violations:
                print(f"    - {violation}")
            exit_code = 1
        else:
            print("\n[PASS] release set is complete with no duplicates")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
