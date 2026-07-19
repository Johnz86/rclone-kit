"""PEP 517 in-tree build backend that forces a platform-specific wheel.

`rclone-kit` bundles a prebuilt, platform-specific rclone executable as
package data (see `scripts/prepare_rclone_artifact.py` and
`rclone_kit.runtime.rclone_binary`). A stock `setuptools.build_meta` backend
still classifies a wheel with no compiled extension module as pure Python
(`py3-none-any`), which would let, for example, a wheel built with the
Windows rclone binary be installed on Linux. Every public PEP 517 hook below
is re-exported unchanged from `setuptools.build_meta`; the only behavior
this module adds is forcing `Distribution.has_ext_modules()` to report
`True`, which is the smallest, most widely used technique to make
`setuptools` emit a platform-tagged wheel (`win_amd64`, `linux_x86_64`, and
so on) without introducing a real compiled extension or reintroducing a
full `setup.py`. `pyproject.toml` selects this module through
`[build-system] build-backend = "_build_backend"` with
`backend-path = ["."]`.
"""

from setuptools.build_meta import *  # noqa: F403
from setuptools.dist import Distribution


def _force_platform_specific_wheels() -> None:
    """Make every `Distribution` report that it has extension modules.

    `setuptools` treats a distribution as pure Python, and therefore tags
    its wheel `py3-none-any`, exactly when both `has_ext_modules()` and
    `has_c_libraries()` return `False`. `rclone-kit` has no compiled
    extension, so this patches `has_ext_modules()` to always return `True`,
    which is the documented, minimal way to opt a data-only distribution
    into platform-specific wheel tagging.
    """
    Distribution.has_ext_modules = lambda _self: True


_force_platform_specific_wheels()
