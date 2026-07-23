"""Unit tests for `rclone_kit.native.library`."""

from pathlib import Path

import pytest

from rclone_kit.native.errors import LibraryNotFoundError
from rclone_kit.native.library import RCLONE_KIT_LIBRARY_ENV_VAR, resolve_library_path


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
