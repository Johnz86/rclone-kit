"""Characterization tests pinning the `Rclone` <-> `RcloneImpl` contract.

`Rclone` is a thin pass-through facade: every public instance method just
forwards to an identically-named `RcloneImpl` method with the same
parameters. These tests exist to catch accidental signature drift (a
renamed/reordered/dropped parameter, or a method losing its `RcloneImpl`
counterpart) before any facade-extraction work moves code between the two,
per docs/implementation_and_build_pipeline.md's "Public facade" roadmap
item: establish the contract with tests before changing a boundary.

`Rclone.upgrade_rclone`/`Rclone.find_rclone_conf` are `@staticmethod`s that
deliberately bypass `RcloneImpl` (they delegate straight to `util.py`/
`config.py`), so they're excluded rather than asserted against.
"""

import inspect
from typing import Any

from rclone_kit import Rclone
from rclone_kit.rclone_impl import RcloneImpl


def _public_instance_methods(cls: type) -> dict[str, Any]:
    return {
        name: member
        for name, member in vars(cls).items()
        if not name.startswith("_") and inspect.isfunction(member)
    }


def test_every_public_rclone_method_has_a_same_named_rcloneimpl_method() -> None:
    rclone_methods = _public_instance_methods(Rclone)
    assert rclone_methods, "expected Rclone to have public instance methods"

    missing = [name for name in rclone_methods if not hasattr(RcloneImpl, name)]

    assert missing == []


# Pre-existing, real asymmetries between the two signatures, found by this
# test rather than designed - recorded here instead of silently ignored, so
# the test still catches new drift everywhere else. Each is a narrower or
# reordered forward, not a positional-argument bug: every `Rclone` call site
# below passes its arguments by keyword, so RcloneImpl.write_text's reversed
# parameter order is harmless at runtime.
#
# - write_text: Rclone's (text, dst) vs RcloneImpl's (dst, text) - same
#   parameters, different order.
# - write_bytes: Rclone doesn't forward RcloneImpl's optional `verbose`.
# - serve_http: Rclone hardcodes `cache_mode="minimal"` rather than exposing
#   it, and doesn't expose `serve_http_log` at all.
_KNOWN_PARTIAL_FORWARDS = frozenset({"write_text", "write_bytes", "serve_http"})


def test_rclone_method_signatures_match_rcloneimpl_counterparts() -> None:
    rclone_methods = _public_instance_methods(Rclone)

    mismatches: dict[str, tuple[list[str], list[str]]] = {}
    for name, rclone_method in rclone_methods.items():
        if name in _KNOWN_PARTIAL_FORWARDS:
            continue
        impl_method = getattr(RcloneImpl, name)
        rclone_params = list(inspect.signature(rclone_method).parameters)
        impl_params = list(inspect.signature(impl_method).parameters)
        if rclone_params != impl_params:
            mismatches[name] = (rclone_params, impl_params)

    assert mismatches == {}


def test_known_partial_forwards_only_expose_parameters_rcloneimpl_actually_has() -> None:
    """Even for the documented exceptions, `Rclone` must never expose a
    parameter name `RcloneImpl` doesn't have - that would be a real drift,
    not just a narrower/reordered forward.
    """
    rclone_methods = _public_instance_methods(Rclone)

    for name in _KNOWN_PARTIAL_FORWARDS:
        rclone_method = rclone_methods[name]
        impl_method = getattr(RcloneImpl, name)
        rclone_params = set(inspect.signature(rclone_method).parameters)
        impl_params = set(inspect.signature(impl_method).parameters)
        assert rclone_params <= impl_params, (
            f"{name}: Rclone exposes parameters RcloneImpl doesn't have: "
            f"{rclone_params - impl_params}"
        )
