"""Public, execution-independent `check()` report type.

A check report is a *comparison* of two filesystems - which paths match,
differ, are missing on one side, or could not be read - not the outcome of
a transfer job. `operation.py`'s docstring declares that module a leaf
holding job-result types, so this type deliberately lives here instead:
`CheckResult` carries no job ids, stats, attempts, or timings, is never
produced by `JobHandle`, and shares no field with `OperationResult`.

This module is a leaf in the same sense as `operation.py`: it imports no
native, RC, client, or subprocess modules, and depends on nothing else in
this package. Wire parsing (the RC JSON -> this type boundary) belongs in
`operations/check_ops_embedded.py`, not here - this is a domain object,
not a wire schema.
"""

from __future__ import annotations

from dataclasses import dataclass

_STATUS_OK = "OK"

_REPORT_FIELD_NAMES = (
    "combined",
    "missing_on_src",
    "missing_on_dst",
    "match",
    "differ",
    "error",
)


def _freeze_paths(name: str, value: object) -> tuple[str, ...]:
    """Freeze one report array into a tuple of strings.

    A bare `str` is rejected explicitly because it is itself iterable:
    `differ="a.txt"` would otherwise silently become one entry per
    character.
    """
    if isinstance(value, str):
        raise ValueError(f"{name} must be an iterable of strings, got a bare string: {value!r}")
    try:
        frozen = tuple(value)  # type: ignore[call-overload]
    except TypeError as error:
        raise ValueError(f"{name} must be an iterable of strings, got {value!r}") from error
    for item in frozen:
        if not isinstance(item, str):
            raise ValueError(f"{name} must contain only strings, got {item!r}")
    return frozen


@dataclass(frozen=True)
class CheckResult:
    """The immutable report of one `check()` comparison of a source and a
    destination.

    Every report array is a tuple of rclone-formatted lines, exactly as
    rclone produced them: `combined` entries carry the one-character
    difference marker `diff()` parses (`=`, `-`, `+`, `*`, `!`), while the
    other arrays hold bare paths. An array rclone did not report - because
    its request flag was off, or because it was on and nothing matched -
    is an empty tuple; the two are not distinguished, since the caller
    already knows which flags it asked for.

    `hash_type` is `None` when rclone reported none, which is the case for
    a download-based check (there is no hash involved). `status` is
    rclone's own textual summary: `"OK"` on success, the error text
    otherwise.
    """

    success: bool
    status: str
    hash_type: str | None = None
    combined: tuple[str, ...] = ()
    missing_on_src: tuple[str, ...] = ()
    missing_on_dst: tuple[str, ...] = ()
    match: tuple[str, ...] = ()
    differ: tuple[str, ...] = ()
    error: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise ValueError(f"success must be a bool, got {self.success!r}")
        if not isinstance(self.status, str) or not self.status:
            raise ValueError(f"status must be a non-empty summary string, got {self.status!r}")
        if self.hash_type is not None and not self.hash_type:
            raise ValueError("hash_type must be None or a non-empty hash name, got an empty string")
        if not self.success and self.status == _STATUS_OK:
            raise ValueError("success=False cannot carry rclone's success status summary")
        for name in _REPORT_FIELD_NAMES:
            object.__setattr__(self, name, _freeze_paths(name, getattr(self, name)))
