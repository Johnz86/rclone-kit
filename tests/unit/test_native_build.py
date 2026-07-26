"""Unit tests for `scripts/native/build.py`'s pure build-tag/profile logic.

Uses `monkeypatch` so these tests exercise tag construction, `CPATH`
handling, and WinFsp SDK detection without invoking a real Go/C toolchain;
the actual end-to-end build is proven separately by running
`scripts/native/build.py` against the real toolchain.
"""

from pathlib import Path

import build as native_build
import pytest

from rclone_kit.runtime.native_platform import (
    LINUX_AMD64_NATIVE_TARGET,
    WINDOWS_AMD64_NATIVE_TARGET,
)


class TestBuildTags:
    def test_no_mount_tags_when_disabled(self) -> None:
        assert native_build._build_tags(WINDOWS_AMD64_NATIVE_TARGET, False) == ()
        assert native_build._tags_args(WINDOWS_AMD64_NATIVE_TARGET, False) == []

    def test_cmount_tag_when_enabled_on_windows(self) -> None:
        assert native_build._build_tags(WINDOWS_AMD64_NATIVE_TARGET, True) == ("cmount",)
        assert native_build._tags_args(WINDOWS_AMD64_NATIVE_TARGET, True) == ["-tags", "cmount"]

    def test_no_tags_on_linux_regardless_of_mount_tags(self) -> None:
        """cmd/mount (bazil.org/fuse) is imported unconditionally on Linux -
        no build tag changes whether it's compiled in."""
        assert native_build._build_tags(LINUX_AMD64_NATIVE_TARGET, True) == ()
        assert native_build._tags_args(LINUX_AMD64_NATIVE_TARGET, True) == []


class TestRequireWinfspFuseIncludeDir:
    def test_returns_the_first_existing_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "missing"
        present = tmp_path / "present"
        present.mkdir()
        monkeypatch.setattr(native_build, "_WINFSP_FUSE_INCLUDE_CANDIDATES", (missing, present))

        assert native_build._require_winfsp_fuse_include_dir() == present

    def test_raises_when_no_candidate_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            native_build,
            "_WINFSP_FUSE_INCLUDE_CANDIDATES",
            (tmp_path / "nope-a", tmp_path / "nope-b"),
        )

        with pytest.raises(native_build.NativeBuildError, match="WinFsp"):
            native_build._require_winfsp_fuse_include_dir()


class TestBuildEnv:
    def test_no_mount_tags_leaves_cpath_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CPATH", raising=False)

        env = native_build._build_env(WINDOWS_AMD64_NATIVE_TARGET, "cc.exe", mount_tags=False)

        assert "CPATH" not in env
        assert env["CGO_ENABLED"] == "1"
        assert env["CC"] == "cc.exe"

    def test_mount_tags_sets_cpath_to_the_fuse_include_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fuse_dir = tmp_path / "fuse"
        fuse_dir.mkdir()
        monkeypatch.setattr(native_build, "_WINFSP_FUSE_INCLUDE_CANDIDATES", (fuse_dir,))
        monkeypatch.delenv("CPATH", raising=False)

        env = native_build._build_env(WINDOWS_AMD64_NATIVE_TARGET, "cc.exe", mount_tags=True)

        assert env["CPATH"] == str(fuse_dir)

    def test_mount_tags_preserves_an_existing_cpath(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fuse_dir = tmp_path / "fuse"
        fuse_dir.mkdir()
        monkeypatch.setattr(native_build, "_WINFSP_FUSE_INCLUDE_CANDIDATES", (fuse_dir,))
        monkeypatch.setenv("CPATH", "/already/there")

        env = native_build._build_env(WINDOWS_AMD64_NATIVE_TARGET, "cc.exe", mount_tags=True)

        assert env["CPATH"] == f"{fuse_dir}{native_build.os.pathsep}/already/there"

    def test_mount_tags_raises_when_no_winfsp_sdk_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_build, "_WINFSP_FUSE_INCLUDE_CANDIDATES", (tmp_path / "nope",))

        with pytest.raises(native_build.NativeBuildError, match="WinFsp"):
            native_build._build_env(WINDOWS_AMD64_NATIVE_TARGET, "cc.exe", mount_tags=True)

    def test_linux_mount_tags_never_needs_winfsp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(native_build, "_WINFSP_FUSE_INCLUDE_CANDIDATES", ())
        monkeypatch.delenv("CPATH", raising=False)

        env = native_build._build_env(LINUX_AMD64_NATIVE_TARGET, "gcc", mount_tags=True)

        assert "CPATH" not in env


class TestLinuxCc:
    def test_resolves_configured_compiler_from_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(native_build.shutil, "which", lambda name: f"/usr/bin/{name}")

        assert native_build._linux_cc({"linux_compiler_cc": "gcc"}) == "/usr/bin/gcc"

    def test_defaults_to_gcc_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(native_build.shutil, "which", lambda name: f"/usr/bin/{name}")

        assert native_build._linux_cc({}) == "/usr/bin/gcc"

    def test_raises_when_not_found_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(native_build.shutil, "which", lambda _name: None)

        with pytest.raises(native_build.NativeBuildError, match="gcc"):
            native_build._linux_cc({"linux_compiler_cc": "gcc"})


class TestBuildNativeTargetProfileValidation:
    def test_rejects_an_unsupported_profile(self, tmp_path: Path) -> None:
        with pytest.raises(native_build.NativeBuildError, match="production"):
            native_build.build_native_target(WINDOWS_AMD64_NATIVE_TARGET, tmp_path, profile="bogus")
