"""Unit tests for `scripts/verify_distribution.py`.

`scripts` is on `sys.path` via `[tool.pytest.ini_options] pythonpath`, so the
script is importable as a plain top-level module, matching how `tests/`
itself is made importable for `helpers.py`.

Every fake "wheel" and "sdist" here is a small in-memory (or `tmp_path`)
archive with controlled contents rather than a real build, so these tests
exercise `verify_distribution`'s file, hash, and metadata-parsing logic
without depending on a real rclone binary or a real `uv build`.
"""

import hashlib
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

import verify_distribution
from rclone_kit.runtime.platform import (
    LINUX_AMD64_ARTIFACT,
    WINDOWS_AMD64_ARTIFACT,
    MachineArchitecture,
    OperatingSystem,
    RcloneArtifact,
)

_FAKE_EXECUTABLE_CONTENT = b"fake-rclone-executable-bytes-for-tests"
_FAKE_EXECUTABLE_DIGEST = hashlib.sha256(_FAKE_EXECUTABLE_CONTENT).hexdigest()
_TAMPERED_EXECUTABLE_CONTENT = b"tampered-rclone-executable-bytes"

_FAKE_WINDOWS_ARTIFACT = RcloneArtifact(
    operating_system=OperatingSystem.WINDOWS,
    architecture=MachineArchitecture.AMD64,
    archive_filename="rclone-fake-windows-amd64.zip",
    download_url="https://example.invalid/rclone-fake-windows-amd64.zip",
    sha256_digest="1" * 64,
    executable_member_name="rclone-fake/rclone.exe",
    executable_name="rclone.exe",
    executable_sha256_digest=_FAKE_EXECUTABLE_DIGEST,
    wheel_platform_tag="win_amd64",
)

_WINDOWS_WHEEL_NAME = "rclone_kit-1.0.0-py3-none-win_amd64.whl"
_LINUX_WHEEL_NAME = "rclone_kit-1.0.0-py3-none-manylinux2014_x86_64.whl"
_UNIVERSAL_WHEEL_NAME = "rclone_kit-1.0.0-py3-none-any.whl"
_UNMAPPABLE_WHEEL_NAME = "rclone_kit-1.0.0-py3-none-linux_aarch64.whl"
_CPYTHON_ABI_WINDOWS_WHEEL_NAME = "rclone_kit-1.0.0-cp313-cp313-win_amd64.whl"


def _write_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for member_name, content in members.items():
            archive.writestr(member_name, content)
    return path


def _write_tar(path: Path, member_names: list[str]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for member_name in member_names:
            info = tarfile.TarInfo(name=member_name)
            info.size = 0
            archive.addfile(info, fileobj=None)
    return path


def _metadata_text(
    requires_python: str | None = ">=3.13", requires_dist: list[str] | None = None
) -> str:
    lines = ["Metadata-Version: 2.4", "Name: rclone_kit", "Version: 1.0.0"]
    if requires_python is not None:
        lines.append(f"Requires-Python: {requires_python}")
    lines.extend(f"Requires-Dist: {requirement}" for requirement in requires_dist or [])
    return "\n".join(lines) + "\n"


def _windows_wheel_members(
    *,
    executable_content: bytes = _FAKE_EXECUTABLE_CONTENT,
    manifest_digest: str = _FAKE_EXECUTABLE_DIGEST,
    include_rclone_kit_license: bool = True,
    include_rclone_license: bool = True,
    requires_python: str | None = ">=3.13",
    requires_dist: list[str] | None = None,
) -> dict[str, bytes]:
    members = {
        "rclone_kit/assets/rclone/win_amd64/rclone.exe": executable_content,
        "rclone_kit/assets/rclone/win_amd64/rclone.exe.sha256": manifest_digest.encode("utf-8"),
        "rclone_kit-1.0.0.dist-info/METADATA": _metadata_text(
            requires_python=requires_python, requires_dist=requires_dist
        ).encode("utf-8"),
    }
    if include_rclone_kit_license:
        members["rclone_kit-1.0.0.dist-info/licenses/LICENSE"] = b"MIT"
    if include_rclone_license:
        members["rclone_kit/assets/rclone/win_amd64/RCLONE_LICENSE"] = b"MIT"
    return members


@pytest.fixture
def fake_windows_artifact(monkeypatch: pytest.MonkeyPatch) -> RcloneArtifact:
    monkeypatch.setattr(
        verify_distribution,
        "resolve_rclone_artifact",
        lambda **_kwargs: _FAKE_WINDOWS_ARTIFACT,
    )
    return _FAKE_WINDOWS_ARTIFACT


@dataclass(frozen=True)
class DenylistCase:
    member_name: str
    expected_denylisted: bool


PYCACHE_MEMBER_IS_DENYLISTED = DenylistCase("rclone_kit/foo/__pycache__/foo.cpython-313.pyc", True)
TESTS_DIRECTORY_MEMBER_IS_DENYLISTED = DenylistCase("rclone_kit/tests/test_foo.py", True)
GIT_DIRECTORY_MEMBER_IS_DENYLISTED = DenylistCase(".git/HEAD", True)
DOTENV_MEMBER_IS_DENYLISTED = DenylistCase("rclone_kit/.env", True)
LOG_FILE_MEMBER_IS_DENYLISTED = DenylistCase("build.log", True)
DS_STORE_MEMBER_IS_DENYLISTED = DenylistCase("rclone_kit/.DS_Store", True)
THUMBS_DB_MEMBER_IS_DENYLISTED = DenylistCase("rclone_kit/Thumbs.db", True)
PYC_SUFFIX_MEMBER_IS_DENYLISTED = DenylistCase("rclone_kit/foo.pyc", True)
ORDINARY_SOURCE_MEMBER_IS_NOT_DENYLISTED = DenylistCase("rclone_kit/cli.py", False)
LATEST_NAMED_MEMBER_IS_NOT_DENYLISTED = DenylistCase("rclone_kit/latest.py", False)

DENYLIST_CASES = [
    PYCACHE_MEMBER_IS_DENYLISTED,
    TESTS_DIRECTORY_MEMBER_IS_DENYLISTED,
    GIT_DIRECTORY_MEMBER_IS_DENYLISTED,
    DOTENV_MEMBER_IS_DENYLISTED,
    LOG_FILE_MEMBER_IS_DENYLISTED,
    DS_STORE_MEMBER_IS_DENYLISTED,
    THUMBS_DB_MEMBER_IS_DENYLISTED,
    PYC_SUFFIX_MEMBER_IS_DENYLISTED,
    ORDINARY_SOURCE_MEMBER_IS_NOT_DENYLISTED,
    LATEST_NAMED_MEMBER_IS_NOT_DENYLISTED,
]
DENYLIST_IDS = [
    "pycache_member_is_denylisted",
    "tests_directory_member_is_denylisted",
    "git_directory_member_is_denylisted",
    "dotenv_member_is_denylisted",
    "log_file_member_is_denylisted",
    "ds_store_member_is_denylisted",
    "thumbs_db_member_is_denylisted",
    "pyc_suffix_member_is_denylisted",
    "ordinary_source_member_is_not_denylisted",
    "latest_named_member_is_not_denylisted",
]


@dataclass(frozen=True)
class RequiresPythonCase:
    specifier: str
    excludes_versions_below_floor: bool


EXACT_FLOOR_SPECIFIER_EXCLUDES_BELOW_FLOOR = RequiresPythonCase(">=3.13", True)
STRICTER_FLOOR_SPECIFIER_EXCLUDES_BELOW_FLOOR = RequiresPythonCase(">=3.14", True)
BOUNDED_FLOOR_SPECIFIER_EXCLUDES_BELOW_FLOOR = RequiresPythonCase(">=3.13,<4", True)
LEGACY_FLOOR_SPECIFIER_ADMITS_BELOW_FLOOR = RequiresPythonCase(">=3.10", False)
UNBOUNDED_SPECIFIER_ADMITS_BELOW_FLOOR = RequiresPythonCase("", False)

REQUIRES_PYTHON_CASES = [
    EXACT_FLOOR_SPECIFIER_EXCLUDES_BELOW_FLOOR,
    STRICTER_FLOOR_SPECIFIER_EXCLUDES_BELOW_FLOOR,
    BOUNDED_FLOOR_SPECIFIER_EXCLUDES_BELOW_FLOOR,
    LEGACY_FLOOR_SPECIFIER_ADMITS_BELOW_FLOOR,
    UNBOUNDED_SPECIFIER_ADMITS_BELOW_FLOOR,
]
REQUIRES_PYTHON_IDS = [
    "exact_floor_specifier_excludes_below_floor",
    "stricter_floor_specifier_excludes_below_floor",
    "bounded_floor_specifier_excludes_below_floor",
    "legacy_floor_specifier_admits_below_floor",
    "unbounded_specifier_admits_below_floor",
]


@pytest.mark.parametrize("case", DENYLIST_CASES, ids=DENYLIST_IDS)
def test_is_denylisted_member(case: DenylistCase) -> None:
    assert verify_distribution._is_denylisted_member(case.member_name) is case.expected_denylisted


@pytest.mark.parametrize("case", REQUIRES_PYTHON_CASES, ids=REQUIRES_PYTHON_IDS)
def test_requires_python_excludes_versions_below_floor(case: RequiresPythonCase) -> None:
    result = verify_distribution._requires_python_excludes_versions_below_floor(case.specifier)
    assert result is case.excludes_versions_below_floor


def test_check_platform_independent_tag_flags_universal_wheel() -> None:
    violations = verify_distribution.check_platform_independent_tag(Path(_UNIVERSAL_WHEEL_NAME))
    assert len(violations) == 1
    assert "platform-independent" in violations[0]


def test_check_platform_independent_tag_passes_platform_specific_wheel() -> None:
    assert verify_distribution.check_platform_independent_tag(Path(_WINDOWS_WHEEL_NAME)) == []


@pytest.mark.usefixtures("fake_windows_artifact")
def test_check_exact_wheel_tag_passes_for_abi_independent_wheel() -> None:
    assert verify_distribution.check_exact_wheel_tag(Path(_WINDOWS_WHEEL_NAME)) == []


@pytest.mark.usefixtures("fake_windows_artifact")
def test_check_exact_wheel_tag_flags_concrete_cpython_abi_tag() -> None:
    violations = verify_distribution.check_exact_wheel_tag(Path(_CPYTHON_ABI_WINDOWS_WHEEL_NAME))

    assert len(violations) == 1
    assert "wheel tag" in violations[0]


def test_check_exact_wheel_tag_skips_unmappable_platform() -> None:
    assert verify_distribution.check_exact_wheel_tag(Path(_UNMAPPABLE_WHEEL_NAME)) == []


def test_check_requires_python_floor_passes_for_313_floor(tmp_path: Path) -> None:
    members = _windows_wheel_members()
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    assert verify_distribution.check_requires_python_floor(wheel_path, tuple(members)) == []


def test_check_requires_python_floor_flags_legacy_floor(tmp_path: Path) -> None:
    members = _windows_wheel_members(requires_python=">=3.10")
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    violations = verify_distribution.check_requires_python_floor(wheel_path, tuple(members))

    assert len(violations) == 1
    assert "Requires-Python" in violations[0]


def test_check_requires_python_floor_flags_missing_metadata() -> None:
    violations = verify_distribution.check_requires_python_floor(Path(_WINDOWS_WHEEL_NAME), ())
    assert len(violations) == 1
    assert "METADATA" in violations[0]


def test_check_no_dev_tools_in_requires_dist_flags_dev_tool(tmp_path: Path) -> None:
    members = _windows_wheel_members(requires_dist=["httpx>=0.28.1", "ruff>=0.15", "pytest-xdist"])
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    violations = verify_distribution.check_no_dev_tools_in_requires_dist(wheel_path, tuple(members))

    assert len(violations) == 2
    assert any("ruff" in violation for violation in violations)
    assert any("pytest-xdist" in violation for violation in violations)


def test_check_no_dev_tools_in_requires_dist_passes_clean_runtime_deps(tmp_path: Path) -> None:
    members = _windows_wheel_members(requires_dist=["httpx>=0.28.1", "boto3>=1.28.23"])
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    assert verify_distribution.check_no_dev_tools_in_requires_dist(wheel_path, tuple(members)) == []


def test_check_required_licenses_present_flags_missing_rclone_kit_license(tmp_path: Path) -> None:
    members = _windows_wheel_members(include_rclone_kit_license=False)
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    violations = verify_distribution.check_required_licenses_present(wheel_path, tuple(members))

    assert any("rclone-kit project license" in violation for violation in violations)


def test_check_required_licenses_present_flags_missing_rclone_license(tmp_path: Path) -> None:
    members = _windows_wheel_members(include_rclone_license=False)
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    violations = verify_distribution.check_required_licenses_present(wheel_path, tuple(members))

    assert any("rclone MIT license" in violation for violation in violations)


def test_check_required_licenses_present_passes_when_both_present(tmp_path: Path) -> None:
    members = _windows_wheel_members()
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    assert verify_distribution.check_required_licenses_present(wheel_path, tuple(members)) == []


def test_check_bundled_executable_present_flags_missing_executable() -> None:
    wheel_path = Path(_WINDOWS_WHEEL_NAME)
    violations = verify_distribution.check_bundled_executable_present(wheel_path, ())
    assert len(violations) == 1
    assert "missing expected bundled executable" in violations[0]


def test_check_bundled_executable_present_passes_when_present() -> None:
    wheel_path = Path(_WINDOWS_WHEEL_NAME)
    members = (
        verify_distribution._asset_member_path(
            WINDOWS_AMD64_ARTIFACT, WINDOWS_AMD64_ARTIFACT.executable_name
        ),
    )
    assert verify_distribution.check_bundled_executable_present(wheel_path, members) == []


def test_check_bundled_executable_present_maps_linux_wheel_tag() -> None:
    wheel_path = Path(_LINUX_WHEEL_NAME)
    members = (
        verify_distribution._asset_member_path(
            LINUX_AMD64_ARTIFACT, LINUX_AMD64_ARTIFACT.executable_name
        ),
    )
    assert verify_distribution.check_bundled_executable_present(wheel_path, members) == []


def test_check_bundled_executable_present_flags_unmappable_platform_tag() -> None:
    wheel_path = Path(_UNMAPPABLE_WHEEL_NAME)
    violations = verify_distribution.check_bundled_executable_present(wheel_path, ())
    assert len(violations) == 1
    assert "does not map to a certified rclone build target" in violations[0]


def test_check_no_foreign_platform_executable_flags_linux_binary_in_windows_wheel() -> None:
    wheel_path = Path(_WINDOWS_WHEEL_NAME)
    members = (
        verify_distribution._asset_member_path(
            WINDOWS_AMD64_ARTIFACT, WINDOWS_AMD64_ARTIFACT.executable_name
        ),
        "rclone_kit/assets/rclone/manylinux2014_x86_64/rclone",
    )
    violations = verify_distribution.check_no_foreign_platform_executable(wheel_path, members)
    assert len(violations) == 1
    assert "another platform's executable" in violations[0]


def test_check_no_foreign_platform_executable_passes_windows_only_wheel() -> None:
    wheel_path = Path(_WINDOWS_WHEEL_NAME)
    members = (
        verify_distribution._asset_member_path(
            WINDOWS_AMD64_ARTIFACT, WINDOWS_AMD64_ARTIFACT.executable_name
        ),
    )
    assert verify_distribution.check_no_foreign_platform_executable(wheel_path, members) == []


@pytest.mark.usefixtures("fake_windows_artifact")
def test_check_bundled_executable_hash_passes_when_both_digests_agree(tmp_path: Path) -> None:
    members = _windows_wheel_members()
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    assert verify_distribution.check_bundled_executable_hash(wheel_path, tuple(members)) == []


@pytest.mark.usefixtures("fake_windows_artifact")
def test_check_bundled_executable_hash_flags_manifest_mismatch(tmp_path: Path) -> None:
    members = _windows_wheel_members(manifest_digest="0" * 64)
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    violations = verify_distribution.check_bundled_executable_hash(wheel_path, tuple(members))

    assert any("shipped manifest" in violation for violation in violations)


@pytest.mark.usefixtures("fake_windows_artifact")
def test_check_bundled_executable_hash_flags_expected_digest_mismatch(tmp_path: Path) -> None:
    tampered_digest = hashlib.sha256(_TAMPERED_EXECUTABLE_CONTENT).hexdigest()
    members = _windows_wheel_members(
        executable_content=_TAMPERED_EXECUTABLE_CONTENT, manifest_digest=tampered_digest
    )
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    violations = verify_distribution.check_bundled_executable_hash(wheel_path, tuple(members))

    assert any("expected digest" in violation for violation in violations)
    assert not any("shipped manifest" in violation for violation in violations)


def test_check_wheel_denylisted_members_flags_pycache() -> None:
    wheel_path = Path(_WINDOWS_WHEEL_NAME)
    members = ("rclone_kit/__pycache__/cli.cpython-313.pyc",)
    violations = verify_distribution.check_wheel_denylisted_members(wheel_path, members)
    assert len(violations) == 1


def test_check_wheel_denylisted_members_passes_clean_tree() -> None:
    wheel_path = Path(_WINDOWS_WHEEL_NAME)
    members = ("rclone_kit/cli.py", "rclone_kit/__init__.py")
    assert verify_distribution.check_wheel_denylisted_members(wheel_path, members) == []


def test_check_sdist_denylisted_members_allows_tests_directory() -> None:
    sdist_path = Path("rclone_kit-1.0.0.tar.gz")
    members = ("rclone_kit-1.0.0/tests/test_foo.py",)
    assert verify_distribution.check_sdist_denylisted_members(sdist_path, members) == []


def test_check_sdist_denylisted_members_flags_git_directory() -> None:
    sdist_path = Path("rclone_kit-1.0.0.tar.gz")
    members = ("rclone_kit-1.0.0/.git/HEAD",)
    violations = verify_distribution.check_sdist_denylisted_members(sdist_path, members)
    assert len(violations) == 1


def test_check_sdist_has_no_staged_platform_executables_flags_bundled_binary() -> None:
    sdist_path = Path("rclone_kit-1.0.0.tar.gz")
    members = ("rclone_kit-1.0.0/src/rclone_kit/assets/rclone/win_amd64/rclone.exe",)
    violations = verify_distribution.check_sdist_has_no_staged_platform_executables(
        sdist_path, members
    )
    assert len(violations) == 1
    assert "platform-independent" in violations[0]


def test_check_sdist_has_no_staged_platform_executables_passes_clean_sdist() -> None:
    sdist_path = Path("rclone_kit-1.0.0.tar.gz")
    members = ("rclone_kit-1.0.0/src/rclone_kit/cli.py",)
    assert (
        verify_distribution.check_sdist_has_no_staged_platform_executables(sdist_path, members)
        == []
    )


def test_check_entry_points_resolve_passes_for_valid_target(tmp_path: Path) -> None:
    members = {
        **_windows_wheel_members(),
        "rclone_kit-1.0.0.dist-info/entry_points.txt": (
            b"[console_scripts]\nfake-cmd = fake_pkg:main\n"
        ),
        "fake_pkg/__init__.py": b"def main() -> int:\n    return 0\n",
    }
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    assert verify_distribution.check_entry_points_resolve(wheel_path, tuple(members)) == []


def test_check_entry_points_resolve_flags_missing_module(tmp_path: Path) -> None:
    members = {
        **_windows_wheel_members(),
        "rclone_kit-1.0.0.dist-info/entry_points.txt": (
            b"[console_scripts]\nfake-cmd = does_not_exist_pkg:main\n"
        ),
    }
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    violations = verify_distribution.check_entry_points_resolve(wheel_path, tuple(members))

    assert len(violations) == 1
    assert "fake-cmd" in violations[0]


def test_check_entry_points_resolve_flags_missing_callable(tmp_path: Path) -> None:
    members = {
        **_windows_wheel_members(),
        "rclone_kit-1.0.0.dist-info/entry_points.txt": (
            b"[console_scripts]\nfake-cmd = fake_pkg:does_not_exist\n"
        ),
        "fake_pkg/__init__.py": b"def main() -> int:\n    return 0\n",
    }
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    violations = verify_distribution.check_entry_points_resolve(wheel_path, tuple(members))

    assert len(violations) == 1
    assert "fake-cmd" in violations[0]


def test_check_entry_points_resolve_flags_missing_entry_points_file() -> None:
    violations = verify_distribution.check_entry_points_resolve(Path(_WINDOWS_WHEEL_NAME), ())
    assert len(violations) == 1
    assert "entry_points.txt" in violations[0]


@pytest.mark.usefixtures("fake_windows_artifact")
def test_verify_wheel_passes_fully_populated_windows_wheel(tmp_path: Path) -> None:
    members = {
        **_windows_wheel_members(),
        "rclone_kit-1.0.0.dist-info/entry_points.txt": (
            b"[console_scripts]\nfake-cmd = fake_pkg:main\n"
        ),
        "fake_pkg/__init__.py": b"def main() -> int:\n    return 0\n",
    }
    wheel_path = _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    result = verify_distribution.verify_wheel(wheel_path)

    assert result.passed, result.violations


def test_verify_sdist_flags_real_tar_gz_bundling_staged_executable(tmp_path: Path) -> None:
    sdist_path = _write_tar(
        tmp_path / "rclone_kit-1.0.0.tar.gz",
        [
            "rclone_kit-1.0.0/src/rclone_kit/cli.py",
            "rclone_kit-1.0.0/src/rclone_kit/assets/rclone/win_amd64/rclone.exe",
        ],
    )

    result = verify_distribution.verify_sdist(sdist_path)

    assert not result.passed
    assert any("platform-independent" in violation for violation in result.violations)


def test_main_returns_nonzero_when_dist_dir_has_no_distribution_files(tmp_path: Path) -> None:
    assert verify_distribution.main([str(tmp_path)]) == 1


def test_main_returns_nonzero_for_not_a_directory(tmp_path: Path) -> None:
    missing_dir = tmp_path / "does-not-exist"
    assert verify_distribution.main([str(missing_dir)]) == 1


def test_check_release_set_passes_with_one_wheel_per_certified_target(tmp_path: Path) -> None:
    (tmp_path / _WINDOWS_WHEEL_NAME).write_bytes(b"")
    (tmp_path / _LINUX_WHEEL_NAME).write_bytes(b"")

    assert verify_distribution.check_release_set(tmp_path) == []


def test_check_release_set_flags_missing_target(tmp_path: Path) -> None:
    (tmp_path / _WINDOWS_WHEEL_NAME).write_bytes(b"")

    violations = verify_distribution.check_release_set(tmp_path)

    assert any(
        "Missing a wheel" in violation and "manylinux2014_x86_64" in violation
        for violation in violations
    )


def test_check_release_set_flags_duplicate_target(tmp_path: Path) -> None:
    (tmp_path / _WINDOWS_WHEEL_NAME).write_bytes(b"")
    (tmp_path / "rclone_kit-1.0.1-py3-none-win_amd64.whl").write_bytes(b"")
    (tmp_path / _LINUX_WHEEL_NAME).write_bytes(b"")

    violations = verify_distribution.check_release_set(tmp_path)

    assert any(
        "Multiple wheels found" in violation and "win_amd64" in violation
        for violation in violations
    )


def test_check_release_set_flags_unrecognized_wheel(tmp_path: Path) -> None:
    (tmp_path / _WINDOWS_WHEEL_NAME).write_bytes(b"")
    (tmp_path / _LINUX_WHEEL_NAME).write_bytes(b"")
    (tmp_path / _UNMAPPABLE_WHEEL_NAME).write_bytes(b"")

    violations = verify_distribution.check_release_set(tmp_path)

    assert any("not a recognized certified-target wheel" in violation for violation in violations)


def test_main_require_complete_release_set_fails_when_incomplete(tmp_path: Path) -> None:
    members = {
        **_windows_wheel_members(),
        "rclone_kit-1.0.0.dist-info/entry_points.txt": (
            b"[console_scripts]\nfake-cmd = fake_pkg:main\n"
        ),
        "fake_pkg/__init__.py": b"def main() -> int:\n    return 0\n",
    }
    _write_zip(tmp_path / _WINDOWS_WHEEL_NAME, members)

    assert verify_distribution.main([str(tmp_path), "--require-complete-release-set"]) == 1
