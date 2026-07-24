"""Unit tests for `rclone_kit.native.library`."""

import json
from pathlib import Path

import pytest

from rclone_kit.native.errors import LibraryNotFoundError, LibraryVerificationError
from rclone_kit.native.library import RCLONE_KIT_LIBRARY_ENV_VAR, resolve_library_path
from rclone_kit.runtime.hashing import sha256_of_file
from rclone_kit.runtime.native_platform import WINDOWS_AMD64_NATIVE_TARGET


def _stage_packaged_asset(assets_root: Path, content: bytes = b"fake-library-bytes") -> Path:
    target_dir = assets_root / WINDOWS_AMD64_NATIVE_TARGET.wheel_platform_tag
    target_dir.mkdir(parents=True)
    library_path = target_dir / WINDOWS_AMD64_NATIVE_TARGET.library_filename
    library_path.write_bytes(content)
    manifest_path = target_dir / "native-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "outputs": [
                    {
                        "filename": WINDOWS_AMD64_NATIVE_TARGET.library_filename,
                        "sha256_digest": sha256_of_file(library_path),
                        "size_bytes": len(content),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return library_path


def test_resolve_library_path_prefers_explicit_path(tmp_path: Path) -> None:
    library = tmp_path / "librclone_kit.dll"
    library.write_bytes(b"")
    assert resolve_library_path(library) == library.resolve()


def test_resolve_library_path_rejects_missing_explicit_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.dll"
    with pytest.raises(LibraryNotFoundError) as excinfo:
        resolve_library_path(missing)
    assert excinfo.value.path == missing.resolve()


def test_resolve_library_path_falls_back_to_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "librclone_kit.dll"
    library.write_bytes(b"")
    monkeypatch.setenv(RCLONE_KIT_LIBRARY_ENV_VAR, str(library))
    assert resolve_library_path() == library.resolve()


def test_resolve_library_path_rejects_missing_env_var_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist.dll"
    monkeypatch.setenv(RCLONE_KIT_LIBRARY_ENV_VAR, str(missing))
    with pytest.raises(LibraryNotFoundError):
        resolve_library_path()


def test_resolve_library_path_raises_with_no_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RCLONE_KIT_LIBRARY_ENV_VAR, raising=False)
    with pytest.raises(LibraryNotFoundError) as excinfo:
        resolve_library_path()
    assert excinfo.value.path is None


def test_resolve_library_path_finds_a_verified_packaged_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(RCLONE_KIT_LIBRARY_ENV_VAR, raising=False)
    library_path = _stage_packaged_asset(tmp_path)

    resolved = resolve_library_path(
        native_target=WINDOWS_AMD64_NATIVE_TARGET, packaged_assets_root=tmp_path
    )

    assert resolved == library_path


def test_resolve_library_path_rejects_a_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(RCLONE_KIT_LIBRARY_ENV_VAR, raising=False)
    library_path = _stage_packaged_asset(tmp_path)
    library_path.write_bytes(b"tampered-bytes")

    with pytest.raises(LibraryVerificationError):
        resolve_library_path(
            native_target=WINDOWS_AMD64_NATIVE_TARGET, packaged_assets_root=tmp_path
        )


def test_resolve_library_path_ignores_packaged_asset_when_manifest_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(RCLONE_KIT_LIBRARY_ENV_VAR, raising=False)
    target_dir = tmp_path / WINDOWS_AMD64_NATIVE_TARGET.wheel_platform_tag
    target_dir.mkdir(parents=True)
    (target_dir / WINDOWS_AMD64_NATIVE_TARGET.library_filename).write_bytes(b"no-manifest")

    with pytest.raises(LibraryNotFoundError):
        resolve_library_path(
            native_target=WINDOWS_AMD64_NATIVE_TARGET, packaged_assets_root=tmp_path
        )


def test_resolve_library_path_explicit_path_wins_over_packaged_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(RCLONE_KIT_LIBRARY_ENV_VAR, raising=False)
    _stage_packaged_asset(tmp_path)
    explicit = tmp_path / "explicit.dll"
    explicit.write_bytes(b"explicit")

    resolved = resolve_library_path(
        explicit, native_target=WINDOWS_AMD64_NATIVE_TARGET, packaged_assets_root=tmp_path
    )

    assert resolved == explicit.resolve()
