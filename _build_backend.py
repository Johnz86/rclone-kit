"""PEP 517 in-tree build backend that forces an ABI-independent,
platform-specific wheel.

`rclone-kit` bundles a prebuilt, platform-specific rclone executable as
package data (see `scripts/prepare_rclone_artifact.py` and
`rclone_kit.runtime.rclone_binary`). A stock `setuptools.build_meta` backend
classifies a wheel with no compiled extension module as pure Python
(`py3-none-any`), which would let, for example, a wheel built with the
Windows rclone binary be installed on Linux. Every public PEP 517 hook below
is re-exported unchanged from `setuptools.build_meta`; the only behavior
this module adds is:

1. Forcing `Distribution.has_ext_modules()` to report `True`, the smallest,
   most widely used technique to make `setuptools` emit a platform-tagged
   wheel (`win_amd64`, `manylinux2014_x86_64`, and so on) without
   introducing a real compiled extension or a full `setup.py`.
2. Overriding `bdist_wheel.get_tag()` so the *interpreter* and *ABI*
   components of that platform-tagged wheel stay `py3`/`none` instead of a
   concrete CPython ABI tag such as `cp313-cp313`. `rclone-kit` ships a
   native executable as data, not a compiled Python extension module, so
   nothing in the wheel is actually CPython-version-specific;
   `Requires-Python >=3.13` in `pyproject.toml` remains the authoritative
   language-version floor. Step 1 alone would otherwise let `setuptools`
   pick the building interpreter's own ABI tag, needlessly pinning the
   wheel to the exact CPython minor version it was built with.
3. Rewriting the Linux platform component from the build host's generic
   `sysconfig` tag (`linux_x86_64`) to the manylinux compatibility tag
   `rclone_kit.runtime.platform.LINUX_AMD64_ARTIFACT` certifies
   (`manylinux2014_x86_64`). This module cannot import
   `rclone_kit.runtime.platform` to read that value directly: at build time
   it runs inside an isolated PEP 517 environment that has only
   `setuptools` installed, never `rclone_kit` or its runtime dependencies
   (see `scripts/build_distribution.py`). The manylinux prefix is
   therefore a small, independently duplicated literal; if it and
   `LINUX_AMD64_ARTIFACT.wheel_platform_tag` ever drift apart,
   `scripts/verify_distribution.py`'s `check_exact_wheel_tag` — which reads
   the authoritative value — fails the build loudly rather than silently
   accepting a stale tag.

`pyproject.toml` selects this module through `[build-system] build-backend
= "_build_backend"` with `backend-path = ["."]`.
"""

from setuptools.build_meta import (
    SetupRequirementsError,
    __legacy__,
    build_editable,
    build_sdist,
    build_wheel,
    get_requires_for_build_editable,
    get_requires_for_build_sdist,
    get_requires_for_build_wheel,
    prepare_metadata_for_build_editable,
    prepare_metadata_for_build_wheel,
)
from setuptools.command.bdist_wheel import bdist_wheel as _bdist_wheel_command
from setuptools.command.bdist_wheel import get_platform as _get_platform
from setuptools.dist import Distribution

__all__ = [
    "SetupRequirementsError",
    "__legacy__",
    "build_editable",
    "build_sdist",
    "build_wheel",
    "get_requires_for_build_editable",
    "get_requires_for_build_sdist",
    "get_requires_for_build_wheel",
    "prepare_metadata_for_build_editable",
    "prepare_metadata_for_build_wheel",
]

_WHEEL_PYTHON_TAG = "py3"
_WHEEL_ABI_TAG = "none"

_LINUX_GENERIC_PLATFORM_PREFIX = "linux_"

_LINUX_MANYLINUX_PLATFORM_PREFIX = "manylinux2014_"


def _force_platform_specific_wheels() -> None:
    """Make every `Distribution` report that it has extension modules.

    `setuptools` treats a distribution as pure Python, and therefore tags
    its wheel `py3-none-any`, exactly when both `has_ext_modules()` and
    `has_c_libraries()` return `False`. `rclone-kit` has no compiled
    extension, so this patches `has_ext_modules()` to always return `True`,
    which is the documented, minimal way to opt a data-only distribution
    into platform-specific wheel tagging.
    """
    Distribution.has_ext_modules = _has_extension_modules


def _has_extension_modules(self: Distribution) -> bool:
    _ = self
    return True


def _normalize_platform_tag(raw_platform_name: str) -> str:
    return raw_platform_name.lower().replace("-", "_").replace(".", "_").replace(" ", "_")


def _certified_platform_tag(normalized_platform_name: str) -> str:
    """Rewrite a generic Linux platform tag (`linux_x86_64`) to the declared
    manylinux compatibility tag (`manylinux2014_x86_64`); every other
    platform tag (`win_amd64`, and so on) passes through unchanged.
    """
    if normalized_platform_name.startswith(_LINUX_GENERIC_PLATFORM_PREFIX):
        suffix = normalized_platform_name.removeprefix(_LINUX_GENERIC_PLATFORM_PREFIX)
        return _LINUX_MANYLINUX_PLATFORM_PREFIX + suffix
    return normalized_platform_name


def _get_tag_without_cpython_abi(self: _bdist_wheel_command) -> tuple[str, str, str]:
    """Return `(python_tag, abi_tag, platform_tag)` for a platform-specific
    wheel that declares no CPython ABI dependency.

    Computes `platform_tag` from the stock, non-pure branch of
    `bdist_wheel.get_tag()` (an explicitly supplied `--plat-name`, else
    `get_platform(self.bdist_dir)`), rewritten to the certified manylinux
    compatibility tag on Linux, but always returns `py3`/`none` for the
    interpreter and ABI components instead of a concrete CPython tag. Does
    not call the stock implementation: it asserts its computed tag is a
    member of `packaging.tags.sys_tags()`, which a deliberately
    interpreter-independent tag never is.
    """
    if self.plat_name_supplied and self.plat_name:
        raw_platform_name = self.plat_name
    else:
        raw_platform_name = _get_platform(self.bdist_dir)
    platform_tag = _certified_platform_tag(_normalize_platform_tag(raw_platform_name))
    return (_WHEEL_PYTHON_TAG, _WHEEL_ABI_TAG, platform_tag)


def _force_abi_independent_wheel_tag() -> None:
    """Replace `bdist_wheel.get_tag` so the built wheel's interpreter and ABI
    tag components are always `py3`/`none`, regardless of the CPython
    version running the build.
    """
    _bdist_wheel_command.get_tag = _get_tag_without_cpython_abi


_force_platform_specific_wheels()
_force_abi_independent_wheel_tag()
