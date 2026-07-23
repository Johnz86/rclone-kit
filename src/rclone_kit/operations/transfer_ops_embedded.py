"""Embedded RC-backed transfer operations (CLI-to-C-ABI migration ledger
rows T01, T02, T07; T11/T12 (`read_bytes`/`read_text`) become transitive
once `copy_to` (T02) is embedded-aware, since `Rclone.read_bytes` only ever
calls `self.copy_to`.

Each operation here starts its RC method as an asynchronous job through the
shared `_JobMonitor` (Wave D Phase D3) - via the same generic `_async: true`
RC mechanism `start_copy()` uses, not a bespoke Go endpoint, since
`operations/copyfile`/`operations/purge`/`operations/cleanup` need no
retry loop of their own - then immediately waits on the resulting handle.
This closes design-review finding F5 (a synchronous RC call here used to
hold the runtime lock for the whole operation) and F6 (these functions now
return a real `OperationResult`, never a synthetic
`subprocess.CompletedProcess`); `Rclone`'s own methods wrap the result via
`CompletedProcess.from_operation_result()` for public-API compatibility.

A failure with `check=True` therefore raises `OperationFailedError` (part
of the execution-independent `OperationError` hierarchy), not a raw
`RcCallError` - closing design-review finding F4 for `copy_to`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from rclone_kit.convert import convert_to_str
from rclone_kit.exceptions import UnsupportedEmbeddedOperationError
from rclone_kit.rc.fs_spec import encode_fs_spec
from rclone_kit.rc.paths import RcPath
from rclone_kit.util import get_check

if TYPE_CHECKING:
    from rclone_kit.config import Config
    from rclone_kit.dir import Dir
    from rclone_kit.file import File
    from rclone_kit.job import _JobMonitor
    from rclone_kit.operation import OperationResult


def _new_group(client_id: uuid.UUID) -> str:
    return f"rclone-kit/{client_id}/{uuid.uuid4()}"


def copy_file_to_embedded(
    monitor: _JobMonitor,
    client_id: uuid.UUID,
    config: Config,
    src: File | str,
    dst: File | str,
    check: bool | None = None,
    other_args: list[str] | None = None,
) -> OperationResult:
    """Copy one file from source to destination via `operations/copyfile`.

    Both sides split at their final path component: `operations/copyfile`
    needs `srcFs`/`dstFs` to be navigable directory roots, never the bare
    file paths themselves. Each side's resulting `fs` value is then passed
    through `encode_fs_spec`, so a configured S3/B2 remote gets rclone's
    RC config-object form (`_name`/`_root`/`no_check_bucket`) instead of a
    plain string - matching the CLI backend's `--s3-no-check-bucket`,
    which has no `_config` equivalent. Every other target is unaffected.

    Raises `OperationFailedError` when `check` resolves `True` (the
    default) and the job fails; otherwise returns an `OperationResult`
    whose `.ok` reflects success, matching the CLI backend's own `check`
    semantics.
    """
    if other_args:
        raise UnsupportedEmbeddedOperationError("copy_to (other_args)")
    check = get_check(check)
    src_str = src if isinstance(src, str) else str(src.path)
    dst_str = dst if isinstance(dst, str) else str(dst.path)
    src_target = RcPath.parse(src_str).as_parent_and_name()
    dst_target = RcPath.parse(dst_str).as_parent_and_name()
    params = {
        "srcFs": encode_fs_spec(config, src_target.fs),
        "srcRemote": src_target.remote,
        "dstFs": encode_fs_spec(config, dst_target.fs),
        "dstRemote": dst_target.remote,
    }
    handle = monitor.start_job(
        "operations/copyfile",
        params,
        group=_new_group(client_id),
        operation="copy_to",
        source=src_str,
        destination=dst_str,
        check=check,
    )
    return handle.wait()


def purge_dir_embedded(
    monitor: _JobMonitor, client_id: uuid.UUID, src: str | Dir
) -> OperationResult:
    """Purge a directory and all of its contents via `operations/purge`.

    Never raises: matches the CLI backend's own `purge_dir`, which always
    returns a result for the caller to inspect via `.ok`.
    """
    src_str = convert_to_str(src)
    target = RcPath.parse(src_str)
    handle = monitor.start_job(
        "operations/purge",
        {"fs": target.fs, "remote": target.remote},
        group=_new_group(client_id),
        operation="purge",
        source=src_str,
        destination=None,
        check=False,
    )
    return handle.wait()


def cleanup_embedded(monitor: _JobMonitor, client_id: uuid.UUID, src: str) -> OperationResult:
    """Remove trashed files in `src` via `operations/cleanup`.

    Never raises, matching the CLI backend's own `cleanup`. `src` is
    reassembled through `RcPath.parse` (rather than forwarded as a raw
    string) so a bare local reference gets absolutized - see
    `RcPath`/`_resolve_local` for why this matters for the shared embedded
    runtime; a configured or inline remote passes through unchanged.
    """
    handle = monitor.start_job(
        "operations/cleanup",
        {"fs": str(RcPath.parse(src))},
        group=_new_group(client_id),
        operation="cleanup",
        source=src,
        destination=None,
        check=False,
    )
    return handle.wait()
