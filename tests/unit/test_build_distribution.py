"""Unit tests for `scripts/build_distribution.py`'s pure orchestration logic.

`scripts` is on `sys.path` via `[tool.pytest.ini_options] pythonpath`, so the
script is importable as a plain top-level module. Only functions that do not
invoke `uv`, the network, or a real venv are covered here; the full
build/verify/smoke-test pipeline is exercised manually per
`docs/release_process.md`, not in the unit suite.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

import build_distribution
from rclone_kit.runtime.native_platform import (
    LINUX_AMD64_NATIVE_TARGET,
    WINDOWS_AMD64_NATIVE_TARGET,
)

_EXPECTED_TARGET_CHOICES = ("windows-amd64", "linux-amd64")


def test_target_choices_lists_every_certified_target() -> None:
    assert build_distribution._target_choices() == _EXPECTED_TARGET_CHOICES


def test_resolve_target_native_target_returns_windows_target() -> None:
    assert (
        build_distribution._resolve_target_native_target("windows-amd64")
        == WINDOWS_AMD64_NATIVE_TARGET
    )


def test_resolve_target_native_target_returns_linux_target() -> None:
    assert (
        build_distribution._resolve_target_native_target("linux-amd64") == LINUX_AMD64_NATIVE_TARGET
    )


def test_resolve_target_native_target_raises_on_malformed_target() -> None:
    with pytest.raises(build_distribution.BuildDistributionError, match="Malformed"):
        build_distribution._resolve_target_native_target("windowsamd64")


def test_resolve_target_native_target_raises_on_unsupported_target() -> None:
    with pytest.raises(build_distribution.BuildDistributionError, match="Unsupported"):
        build_distribution._resolve_target_native_target("windows-arm64")


def test_require_running_on_target_platform_passes_when_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        build_distribution, "_resolve_running_native_target", lambda: WINDOWS_AMD64_NATIVE_TARGET
    )
    build_distribution._require_running_on_target_platform(WINDOWS_AMD64_NATIVE_TARGET)


def test_require_running_on_target_platform_raises_when_mismatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        build_distribution, "_resolve_running_native_target", lambda: LINUX_AMD64_NATIVE_TARGET
    )
    with pytest.raises(build_distribution.BuildDistributionError, match="does not cross-compile"):
        build_distribution._require_running_on_target_platform(WINDOWS_AMD64_NATIVE_TARGET)


def test_prepare_output_directory_creates_missing_directory(tmp_path: Path) -> None:
    out_dir = tmp_path / "dist"

    result = build_distribution._prepare_output_directory(out_dir)

    assert result == out_dir.resolve()
    assert result.is_dir()


def test_prepare_output_directory_accepts_existing_empty_directory(tmp_path: Path) -> None:
    out_dir = tmp_path / "dist"
    out_dir.mkdir()

    assert build_distribution._prepare_output_directory(out_dir) == out_dir.resolve()


def test_prepare_output_directory_raises_when_nonempty(tmp_path: Path) -> None:
    out_dir = tmp_path / "dist"
    out_dir.mkdir()
    (out_dir / "stale.whl").write_bytes(b"stale")

    with pytest.raises(build_distribution.BuildDistributionError, match="not empty"):
        build_distribution._prepare_output_directory(out_dir)


def test_prepare_output_directory_creates_temp_dir_when_none_given() -> None:
    result = build_distribution._prepare_output_directory(None)

    assert result.is_dir()
    assert list(result.iterdir()) == []


def test_wheel_names_returns_only_whl_files(tmp_path: Path) -> None:
    (tmp_path / "rclone_kit-1.0.0-py3-none-win_amd64.whl").write_bytes(b"")
    (tmp_path / "rclone_kit-1.0.0.tar.gz").write_bytes(b"")

    assert build_distribution._wheel_names(tmp_path) == {"rclone_kit-1.0.0-py3-none-win_amd64.whl"}


def test_ignore_build_cruft_filters_pycache_and_egg_info() -> None:
    names = ["util.py", "__pycache__", "rclone_kit.egg-info", "assets"]

    assert build_distribution._ignore_build_cruft(".", names) == {
        "__pycache__",
        "rclone_kit.egg-info",
    }


@dataclass(frozen=True)
class SmokeEnvLayoutCase:
    platform: str
    expected_python: Path
    expected_scripts_dir: Path


WINDOWS_SMOKE_ENV_USES_SCRIPTS_DIRECTORY = SmokeEnvLayoutCase(
    "win32", Path("env/Scripts/python.exe"), Path("env/Scripts")
)
LINUX_SMOKE_ENV_USES_BIN_DIRECTORY = SmokeEnvLayoutCase(
    "linux", Path("env/bin/python"), Path("env/bin")
)

SMOKE_ENV_LAYOUT_CASES = [
    WINDOWS_SMOKE_ENV_USES_SCRIPTS_DIRECTORY,
    LINUX_SMOKE_ENV_USES_BIN_DIRECTORY,
]
SMOKE_ENV_LAYOUT_IDS = [
    "windows_smoke_env_uses_scripts_directory",
    "linux_smoke_env_uses_bin_directory",
]


@pytest.mark.parametrize("case", SMOKE_ENV_LAYOUT_CASES, ids=SMOKE_ENV_LAYOUT_IDS)
def test_smoke_env_python_and_scripts_dir(
    case: SmokeEnvLayoutCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_distribution.sys, "platform", case.platform)

    python_executable, scripts_dir = build_distribution._smoke_env_python_and_scripts_dir(
        Path("env")
    )

    assert python_executable == case.expected_python
    assert scripts_dir == case.expected_scripts_dir


def test_poisoned_proxy_env_overrides_proxy_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://real-proxy.invalid")
    monkeypatch.setenv("SOME_UNRELATED_VAR", "kept")

    env = build_distribution._poisoned_proxy_env()

    assert env["HTTP_PROXY"] == "http://127.0.0.1:1"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:1"
    assert env["NO_PROXY"] == ""
    assert env["SOME_UNRELATED_VAR"] == "kept"


def test_python_version_reads_stripped_file_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version_file = tmp_path / ".python-version"
    version_file.write_text("3.13\n", encoding="utf-8")
    monkeypatch.setattr(build_distribution, "_PYTHON_VERSION_FILE", version_file)

    assert build_distribution._python_version() == "3.13"


def test_copy_source_tree_copies_declared_entries_and_skips_cruft(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    (source_root / "src" / "rclone_kit").mkdir(parents=True)
    (source_root / "src" / "rclone_kit" / "__init__.py").write_text("", encoding="utf-8")
    (source_root / "src" / "rclone_kit" / "__pycache__").mkdir()
    (source_root / "src" / "rclone_kit" / "__pycache__" / "stale.pyc").write_bytes(b"")
    (source_root / "pyproject.toml").write_text("", encoding="utf-8")
    (source_root / "_build_backend.py").write_text("", encoding="utf-8")
    (source_root / "README.md").write_text("", encoding="utf-8")
    (source_root / "LICENSE").write_text("", encoding="utf-8")
    destination = tmp_path / "copy"

    build_distribution._copy_source_tree(source_root, destination)

    assert (destination / "src" / "rclone_kit" / "__init__.py").is_file()
    assert (destination / "pyproject.toml").is_file()
    assert not (destination / "src" / "rclone_kit" / "__pycache__").exists()


def test_built_wheel_is_frozen_dataclass() -> None:
    built_wheel = build_distribution.BuiltWheel(Path("dist/x.whl"), "0" * 64)

    assert built_wheel.path == Path("dist/x.whl")
    assert built_wheel.sha256_digest == "0" * 64


def test_stage_native_bundle_copies_package_data_and_excludes_diagnostics(
    tmp_path: Path,
) -> None:
    native_build_dir = tmp_path / "native-build"
    native_build_dir.mkdir()
    (native_build_dir / WINDOWS_AMD64_NATIVE_TARGET.library_filename).write_bytes(b"lib")
    (native_build_dir / "native-manifest.json").write_text("{}", encoding="utf-8")
    (native_build_dir / "SHA256SUMS").write_text("", encoding="utf-8")
    (native_build_dir / "RCLONE_LICENSE").write_text("", encoding="utf-8")
    (native_build_dir / "rclonekit_abi.h").write_text("", encoding="utf-8")
    (native_build_dir / WINDOWS_AMD64_NATIVE_TARGET.executable_filename).write_bytes(b"exe")
    (native_build_dir / "smoke-results.json").write_text("{}", encoding="utf-8")
    source_tree = tmp_path / "source"
    source_tree.mkdir()

    build_distribution._stage_native_bundle_into_source_tree(
        WINDOWS_AMD64_NATIVE_TARGET, native_build_dir, source_tree
    )

    staged_dir = (
        source_tree
        / "src"
        / "rclone_kit"
        / "assets"
        / "native"
        / WINDOWS_AMD64_NATIVE_TARGET.wheel_platform_tag
    )
    assert (staged_dir / WINDOWS_AMD64_NATIVE_TARGET.library_filename).is_file()
    assert (staged_dir / "native-manifest.json").is_file()
    assert (staged_dir / "SHA256SUMS").is_file()
    assert (staged_dir / "RCLONE_LICENSE").is_file()
    assert (staged_dir / "rclonekit_abi.h").is_file()
    assert not (staged_dir / WINDOWS_AMD64_NATIVE_TARGET.executable_filename).exists()
    assert not (staged_dir / "smoke-results.json").exists()
