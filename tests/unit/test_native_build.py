"""Unit tests for `scripts/native/build.py`'s pure build-tag/profile logic.

Uses `monkeypatch` so these tests exercise tag construction, `CPATH`
handling, and WinFsp SDK detection without invoking a real Go/C toolchain;
the actual end-to-end build is proven separately by running
`scripts/native/build.py` against the real toolchain (see
`native_c_abi_wave_h_review_and_design.md`'s mount addendum).
"""

from pathlib import Path

import build as native_build
import pytest

from rclone_kit.runtime.native_platform import WINDOWS_AMD64_NATIVE_TARGET


class TestBuildTags:
    def test_no_mount_tags_when_disabled(self) -> None:
        assert native_build._build_tags(False) == ()
        assert native_build._tags_args(False) == []

    def test_cmount_tag_when_enabled(self) -> None:
        assert native_build._build_tags(True) == ("cmount",)
        assert native_build._tags_args(True) == ["-tags", "cmount"]


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

        env = native_build._build_env("cc.exe", mount_tags=False)

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

        env = native_build._build_env("cc.exe", mount_tags=True)

        assert env["CPATH"] == str(fuse_dir)

    def test_mount_tags_preserves_an_existing_cpath(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fuse_dir = tmp_path / "fuse"
        fuse_dir.mkdir()
        monkeypatch.setattr(native_build, "_WINFSP_FUSE_INCLUDE_CANDIDATES", (fuse_dir,))
        monkeypatch.setenv("CPATH", "/already/there")

        env = native_build._build_env("cc.exe", mount_tags=True)

        assert env["CPATH"] == f"{fuse_dir}{native_build.os.pathsep}/already/there"

    def test_mount_tags_raises_when_no_winfsp_sdk_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_build, "_WINFSP_FUSE_INCLUDE_CANDIDATES", (tmp_path / "nope",))

        with pytest.raises(native_build.NativeBuildError, match="WinFsp"):
            native_build._build_env("cc.exe", mount_tags=True)


class TestBuildNativeTargetProfileValidation:
    def test_rejects_an_unsupported_profile(self, tmp_path: Path) -> None:
        with pytest.raises(native_build.NativeBuildError, match="production"):
            native_build.build_native_target(WINDOWS_AMD64_NATIVE_TARGET, tmp_path, profile="bogus")
