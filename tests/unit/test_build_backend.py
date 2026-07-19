"""Unit tests for `_build_backend.py`'s wheel-tagging overrides.

`_build_backend.py` lives at the repo root and is importable via the "."
entry in `[tool.pytest.ini_options] pythonpath`. `_get_tag_without_cpython_abi`
is exercised by calling it with a small stand-in object rather than a real
`bdist_wheel` command instance, since it only reads
`plat_name_supplied`/`plat_name`/`bdist_dir` off `self`.
"""

from dataclasses import dataclass
from typing import cast

import _build_backend
import pytest
from setuptools.command.bdist_wheel import bdist_wheel

_LINUX_HOST_PLATFORM_NAME = "linux-x86_64"
_LINUX_CERTIFIED_TAG = "manylinux2014_x86_64"
_WINDOWS_HOST_PLATFORM_NAME = "win-amd64"
_WINDOWS_CERTIFIED_TAG = "win_amd64"


@dataclass(frozen=True)
class CertifiedPlatformTagCase:
    normalized_platform_name: str
    expected_tag: str


LINUX_GENERIC_TAG_BECOMES_MANYLINUX = CertifiedPlatformTagCase("linux_x86_64", _LINUX_CERTIFIED_TAG)
WINDOWS_TAG_PASSES_THROUGH_UNCHANGED = CertifiedPlatformTagCase("win_amd64", _WINDOWS_CERTIFIED_TAG)

CERTIFIED_PLATFORM_TAG_CASES = [
    LINUX_GENERIC_TAG_BECOMES_MANYLINUX,
    WINDOWS_TAG_PASSES_THROUGH_UNCHANGED,
]
CERTIFIED_PLATFORM_TAG_IDS = [
    "linux_generic_tag_becomes_manylinux",
    "windows_tag_passes_through_unchanged",
]


@pytest.mark.parametrize("case", CERTIFIED_PLATFORM_TAG_CASES, ids=CERTIFIED_PLATFORM_TAG_IDS)
def test_certified_platform_tag(case: CertifiedPlatformTagCase) -> None:
    assert (
        _build_backend._certified_platform_tag(case.normalized_platform_name) == case.expected_tag
    )


@dataclass
class _StubBdistWheelCommand:
    plat_name_supplied: bool
    plat_name: str | None
    bdist_dir: str = "unused"


def test_get_tag_without_cpython_abi_rewrites_linux_host_tag_to_manylinux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _build_backend, "_get_platform", lambda _bdist_dir: _LINUX_HOST_PLATFORM_NAME
    )
    stub = _StubBdistWheelCommand(plat_name_supplied=False, plat_name=None)

    tag = _build_backend._get_tag_without_cpython_abi(cast(bdist_wheel, stub))

    assert tag == ("py3", "none", _LINUX_CERTIFIED_TAG)


def test_get_tag_without_cpython_abi_leaves_windows_host_tag_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _build_backend, "_get_platform", lambda _bdist_dir: _WINDOWS_HOST_PLATFORM_NAME
    )
    stub = _StubBdistWheelCommand(plat_name_supplied=False, plat_name=None)

    tag = _build_backend._get_tag_without_cpython_abi(cast(bdist_wheel, stub))

    assert tag == ("py3", "none", _WINDOWS_CERTIFIED_TAG)


def test_get_tag_without_cpython_abi_prefers_explicit_plat_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(_bdist_dir: str) -> str:
        raise AssertionError("get_platform must not run when plat_name is explicitly supplied")

    monkeypatch.setattr(_build_backend, "_get_platform", fail_if_called)
    stub = _StubBdistWheelCommand(plat_name_supplied=True, plat_name="linux-x86_64")

    tag = _build_backend._get_tag_without_cpython_abi(cast(bdist_wheel, stub))

    assert tag == ("py3", "none", _LINUX_CERTIFIED_TAG)
