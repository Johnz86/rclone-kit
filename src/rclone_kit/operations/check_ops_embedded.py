"""Embedded RC-backed verification (`operations/check`).

Unlike every operation in `transfer_ops_embedded.py`, this runs as one
direct, synchronous `RcClient` call rather than as an asynchronous job
through `_JobMonitor` - deliberately, even though a check over a large
tree can run for a long time:

- The report *is* the RC call's `output`, and the job types deliberately
  do not surface that. `_JobMonitor._settle_terminal` parses only
  `attempts` out of a finished job's output, and neither `JobHandle` nor
  `OperationResult` exposes the rest of it. A job-based check would
  therefore need a new raw-output channel threaded through `JobStatus` ->
  `OperationResult` for this one caller's benefit, widening two public
  types to carry untyped RC JSON - the exact thing `operation.py` exists
  to keep out.
- A long synchronous call blocks nothing but its own caller.
  `RcloneRuntime.call` explicitly does not hold a lock for the duration
  of a call (see its class docstring), so an in-flight check never delays
  another client's RC traffic, another job's status polling, or the
  monitor thread.

The accepted trade-off is that a check has no `JobHandle`: it cannot be
cancelled or progress-polled, and a caller that wants a bounded wait must
impose one itself. Nothing about that is cheaper in the job-based design -
`operations/check` reports nothing until it finishes either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rclone_kit.check import CheckResult
from rclone_kit.operations.transfer_options import TransferOptions, encode_transfer_options_config
from rclone_kit.rc.fs_spec import encode_fs_spec

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rclone_kit.config import Config
    from rclone_kit.rc.client import RcCallable

CHECK_METHOD = "operations/check"

# RC parameter name per `CheckResult` report array, in the order
# `fs/operations/rc.go`'s `rcCheck` registers them.
_REPORT_ARRAY_PARAMS = {
    "combined": "combined",
    "missing_on_src": "missingOnSrc",
    "missing_on_dst": "missingOnDst",
    "match": "match",
    "differ": "differ",
    "error": "error",
}


def _report_array(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    """Read one report array out of an `operations/check` response.

    rclone omits an array entirely when its request flag was off, so an
    absent key is an empty report, not a malformed response. A present
    value that is not a JSON array is malformed and raises.
    """
    value = payload.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{key!r} must be an array of strings in an {CHECK_METHOD} response")
    return tuple(value)


def parse_check_result(payload: Mapping[str, object]) -> CheckResult:
    """Parse one `operations/check` response into a `CheckResult`.

    `success`/`status` are always reported by rclone, so a response
    missing either is malformed and raises through `CheckResult`'s own
    validation rather than being silently defaulted. `hashType` genuinely
    may be missing (a download-based check involves no hash) and maps to
    `None`.
    """
    hash_type = payload.get("hashType")
    return CheckResult(
        success=bool(payload.get("success", False)),
        status=str(payload.get("status", "")),
        hash_type=None if hash_type is None else str(hash_type),
        **{
            field_name: _report_array(payload, param)
            for field_name, param in _REPORT_ARRAY_PARAMS.items()
        },
    )


def _check_config_overlay(
    *, size_only: bool | None, fast_list: bool, checkers: int | None
) -> dict[str, object]:
    """Build the `_config` overlay for a check.

    `checkers` goes through `TransferOptions` rather than being written
    straight into the overlay, so it gets the same validation and the same
    `fs.ConfigInfo` key mapping every transfer call already uses.
    `SizeOnly`/`UseListR` have no `TransferOptions` field and are set
    directly.
    """
    overlay = encode_transfer_options_config(TransferOptions(checkers=checkers))
    if size_only is not None:
        overlay["SizeOnly"] = size_only
    if fast_list:
        overlay["UseListR"] = True
    return overlay


def run_check_embedded(
    rc_client: RcCallable,
    config: Config,
    src: str,
    dst: str,
    *,
    one_way: bool = False,
    download: bool = False,
    combined: bool | None = None,
    missing_on_src: bool | None = None,
    missing_on_dst: bool | None = None,
    match: bool | None = None,
    differ: bool | None = None,
    error: bool | None = None,
    size_only: bool | None = None,
    fast_list: bool = False,
    checkers: int | None = None,
) -> CheckResult:
    """Compare `src` and `dst` via one `operations/check` call and return
    the typed report.

    Each report flag left `None` is omitted from the request, so rclone's
    own documented defaults apply (`fs/operations/rc.go`): `combined` and
    `match` off, `missingOnSrc`, `missingOnDst`, `differ`, and `error` on.
    Both sides pass through `encode_fs_spec`, so a configured S3/B2 remote
    gets rclone's RC config-object form exactly as a copy or sync would.

    Never raises for a difference: a source and destination that do not
    match is a successful call reporting `success=False`. An RC-level
    failure (bad path, missing backend, ...) still raises `RcCallError`.
    """
    params: dict[str, object] = {
        "srcFs": encode_fs_spec(config, src),
        "dstFs": encode_fs_spec(config, dst),
        "oneWay": one_way,
        "download": download,
    }
    requested = {
        "combined": combined,
        "missingOnSrc": missing_on_src,
        "missingOnDst": missing_on_dst,
        "match": match,
        "differ": differ,
        "error": error,
    }
    params.update({name: flag for name, flag in requested.items() if flag is not None})
    config_overlay = _check_config_overlay(
        size_only=size_only, fast_list=fast_list, checkers=checkers
    )
    if config_overlay:
        params["_config"] = config_overlay
    return parse_check_result(rc_client.call(CHECK_METHOD, **params))
