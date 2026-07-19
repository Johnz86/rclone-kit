"""Unit tests for `rclone_kit.runtime.rclone_binary`.

No test in this file touches a real packaged wheel asset, the real network,
or `PATH`; every dependency is either a synthetic fixture under `tmp_path` or
a monkeypatched stand-in.
"""

import hashlib
from pathlib import Path

import pytest

from rclone_kit.runtime import rclone_binary
from rclone_kit.runtime.exceptions import (
    CacheReplacementError,
    CacheVerificationError,
    ExplicitExecutableNotFoundError,
    RcloneResolutionError,
)
from rclone_kit.runtime.platform import MachineArchitecture, OperatingSystem, RcloneArtifact
from rclone_kit.runtime.rclone_binary import resolve_rclone_executable

_EXECUTABLE_CONTENT = b"fake-rclone-binary-bytes"
_EXECUTABLE_DIGEST = hashlib.sha256(_EXECUTABLE_CONTENT).hexdigest()

_TEST_ARTIFACT = RcloneArtifact(
    operating_system=OperatingSystem.LINUX,
    architecture=MachineArchitecture.AMD64,
    archive_filename="rclone-test-linux-amd64.zip",
    download_url="https://example.invalid/rclone-test-linux-amd64.zip",
    sha256_digest="1" * 64,
    executable_member_name="rclone-test/rclone",
    executable_name="rclone",
    executable_sha256_digest="2" * 64,
    wheel_platform_tag="manylinux2014_x86_64",
)


def _stage_bundled_asset(assets_root: Path, content: bytes, digest: str) -> None:
    platform_dir = assets_root / _TEST_ARTIFACT.wheel_platform_tag
    platform_dir.mkdir(parents=True, exist_ok=True)
    executable_path = platform_dir / _TEST_ARTIFACT.executable_name
    executable_path.write_bytes(content)
    manifest_path = executable_path.with_name(executable_path.name + ".sha256")
    manifest_path.write_text(digest, encoding="utf-8")


def test_explicit_path_is_returned_when_it_exists(tmp_path: Path) -> None:
    executable = tmp_path / "rclone"
    executable.write_bytes(_EXECUTABLE_CONTENT)

    result = resolve_rclone_executable(explicit_path=executable)

    assert result == executable.resolve()


def test_explicit_path_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(ExplicitExecutableNotFoundError):
        resolve_rclone_executable(explicit_path=tmp_path / "does-not-exist")


def test_explicit_path_raises_when_it_is_a_directory(tmp_path: Path) -> None:
    directory = tmp_path / "a-directory"
    directory.mkdir()

    with pytest.raises(ExplicitExecutableNotFoundError):
        resolve_rclone_executable(explicit_path=directory)


def test_bundled_asset_is_materialized_into_cache(tmp_path: Path) -> None:
    assets_root = tmp_path / "assets"
    cache_root = tmp_path / "cache"
    _stage_bundled_asset(assets_root, _EXECUTABLE_CONTENT, _EXECUTABLE_DIGEST)

    result = resolve_rclone_executable(
        artifact=_TEST_ARTIFACT, packaged_assets_root=assets_root, cache_root=cache_root
    )

    assert result == cache_root / _TEST_ARTIFACT.wheel_platform_tag / _TEST_ARTIFACT.executable_name
    assert result.read_bytes() == _EXECUTABLE_CONTENT


def test_bundled_asset_corrupt_content_raises_cache_verification_error(tmp_path: Path) -> None:
    assets_root = tmp_path / "assets"
    cache_root = tmp_path / "cache"
    _stage_bundled_asset(assets_root, b"tampered-bytes", _EXECUTABLE_DIGEST)

    with pytest.raises(CacheVerificationError):
        resolve_rclone_executable(
            artifact=_TEST_ARTIFACT, packaged_assets_root=assets_root, cache_root=cache_root
        )


def test_reuses_existing_valid_cache_entry_without_recopying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets_root = tmp_path / "assets"
    cache_root = tmp_path / "cache"
    _stage_bundled_asset(assets_root, _EXECUTABLE_CONTENT, _EXECUTABLE_DIGEST)

    first_result = resolve_rclone_executable(
        artifact=_TEST_ARTIFACT, packaged_assets_root=assets_root, cache_root=cache_root
    )
    assert first_result.read_bytes() == _EXECUTABLE_CONTENT

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("shutil.copyfile must not run when the cache entry is already valid")

    monkeypatch.setattr(rclone_binary.shutil, "copyfile", fail_if_called)

    second_result = resolve_rclone_executable(
        artifact=_TEST_ARTIFACT, packaged_assets_root=assets_root, cache_root=cache_root
    )

    assert second_result == first_result
    assert second_result.read_bytes() == _EXECUTABLE_CONTENT


def test_replaces_existing_invalid_cache_entry(tmp_path: Path) -> None:
    assets_root = tmp_path / "assets"
    cache_root = tmp_path / "cache"
    _stage_bundled_asset(assets_root, _EXECUTABLE_CONTENT, _EXECUTABLE_DIGEST)

    stale_cache_path = (
        cache_root / _TEST_ARTIFACT.wheel_platform_tag / _TEST_ARTIFACT.executable_name
    )
    stale_cache_path.parent.mkdir(parents=True)
    stale_cache_path.write_bytes(b"stale-and-wrong-content")

    result = resolve_rclone_executable(
        artifact=_TEST_ARTIFACT, packaged_assets_root=assets_root, cache_root=cache_root
    )

    assert result.read_bytes() == _EXECUTABLE_CONTENT


def test_atomic_replacement_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets_root = tmp_path / "assets"
    cache_root = tmp_path / "cache"
    _stage_bundled_asset(assets_root, _EXECUTABLE_CONTENT, _EXECUTABLE_DIGEST)

    def failing_atomic_replace(_temp_path: Path, destination: Path) -> None:
        raise CacheReplacementError(destination)

    monkeypatch.setattr(rclone_binary, "atomic_replace_file", failing_atomic_replace)

    with pytest.raises(CacheReplacementError):
        resolve_rclone_executable(
            artifact=_TEST_ARTIFACT, packaged_assets_root=assets_root, cache_root=cache_root
        )


def test_no_strategy_available_raises_resolution_error(tmp_path: Path) -> None:
    assets_root = tmp_path / "empty-assets"
    cache_root = tmp_path / "cache"

    with pytest.raises(RcloneResolutionError):
        resolve_rclone_executable(
            artifact=_TEST_ARTIFACT, packaged_assets_root=assets_root, cache_root=cache_root
        )


def test_path_lookup_is_not_attempted_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets_root = tmp_path / "empty-assets"
    cache_root = tmp_path / "cache"

    def fail_if_called(_name: str) -> str | None:
        raise AssertionError("shutil.which must not run unless allow_path_lookup=True")

    monkeypatch.setattr(rclone_binary.shutil, "which", fail_if_called)

    with pytest.raises(RcloneResolutionError):
        resolve_rclone_executable(
            artifact=_TEST_ARTIFACT, packaged_assets_root=assets_root, cache_root=cache_root
        )


def test_path_lookup_is_used_when_explicitly_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets_root = tmp_path / "empty-assets"
    cache_root = tmp_path / "cache"
    found_executable = tmp_path / "on-path" / "rclone"
    found_executable.parent.mkdir(parents=True)
    found_executable.write_bytes(_EXECUTABLE_CONTENT)

    monkeypatch.setattr(rclone_binary.shutil, "which", lambda _name: str(found_executable))

    result = resolve_rclone_executable(
        artifact=_TEST_ARTIFACT,
        packaged_assets_root=assets_root,
        cache_root=cache_root,
        allow_path_lookup=True,
    )

    assert result == found_executable.resolve()


def test_verified_download_fallback_is_used_when_explicitly_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets_root = tmp_path / "empty-assets"
    cache_root = tmp_path / "cache"

    def fake_fetch_verified_archive(_artifact: RcloneArtifact, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake-archive-bytes")
        return destination

    def fake_extract_single_member(
        _archive_path: Path, _member_name: str, destination: Path
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_EXECUTABLE_CONTENT)
        return destination

    monkeypatch.setattr(rclone_binary, "fetch_verified_archive", fake_fetch_verified_archive)
    monkeypatch.setattr(rclone_binary, "extract_single_member", fake_extract_single_member)

    result = resolve_rclone_executable(
        artifact=_TEST_ARTIFACT,
        packaged_assets_root=assets_root,
        cache_root=cache_root,
        allow_verified_download=True,
    )

    assert result.read_bytes() == _EXECUTABLE_CONTENT
