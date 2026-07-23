"""Embedded RC-backed transfer operations (CLI-to-C-ABI migration ledger
rows T01, T02, T06, T07, T08; T11/T12 (`read_bytes`/`read_text`) become
transitive once `copy_to` (T02) is embedded-aware, since `Rclone.read_bytes`
only ever calls `self.copy_to`.

T01/T02/T07 each start their RC method as an asynchronous job through the
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

T06/T08 (`copy_files_embedded`/`delete_files_embedded`, Wave E) are
composite: each partitions its file list exactly like the CLI backend
does (`group_files()`, unchanged), starts one job per partition, waits on
every partition (never aborting collection early on a partial failure -
see `native_c_abi_wave_e_review_and_design.md` section 3, decision E3),
and folds every partition's `OperationResult` into a single aggregate via
`_aggregate_results()` before optionally raising once, for the aggregate
as a whole.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from rclone_kit.convert import convert_to_str
from rclone_kit.exceptions import (
    OperationCancelledError,
    OperationFailedError,
    UnsupportedEmbeddedOperationError,
)
from rclone_kit.group_files import group_files
from rclone_kit.operation import OperationResult, OperationWarning, TransferStats
from rclone_kit.operations.transfer_options import TransferOptions, encode_transfer_options_config
from rclone_kit.rc.fs_spec import encode_fs_spec
from rclone_kit.rc.paths import RcPath
from rclone_kit.util import get_check, write_files_from

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rclone_kit.config import Config
    from rclone_kit.dir import Dir
    from rclone_kit.file import File
    from rclone_kit.job import _JobMonitor

_COPY_FILES_DEFAULT_CHECKERS = 1000
_COPY_FILES_DEFAULT_TRANSFERS = 32
_COPY_FILES_DEFAULT_LOW_LEVEL_RETRIES = 10
_COPY_FILES_DEFAULT_RETRIES = 3
_DELETE_FILES_CHECKERS = 1000
_DELETE_FILES_TRANSFERS = 1000


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


def _sum_stats(stats: Sequence[TransferStats]) -> TransferStats:
    """Combine every partition's final stats into one snapshot.

    Only genuinely cumulative counters (bytes/checks/transfers/errors) are
    summed; `speed`/`eta_seconds`/`elapsed_seconds` describe a single,
    already-finished job's own timeline and are not meaningfully summable
    across partitions that ran concurrently, so they are reported as
    `0.0`/`None` in the aggregate rather than inflated by partition count.
    """
    return TransferStats(
        bytes=sum(s.bytes for s in stats),
        total_bytes=sum(s.total_bytes for s in stats),
        checks=sum(s.checks for s in stats),
        total_checks=sum(s.total_checks for s in stats),
        transfers=sum(s.transfers for s in stats),
        total_transfers=sum(s.total_transfers for s in stats),
        errors=sum(s.errors for s in stats),
        fatal_error=any(s.fatal_error for s in stats),
        retry_error=any(s.retry_error for s in stats),
        speed=0.0,
        eta_seconds=None,
        elapsed_seconds=0.0,
    )


def _aggregate_results(
    operation: str,
    source: str | None,
    destination: str | None,
    results: Sequence[OperationResult],
) -> OperationResult:
    """Fold every partition's `OperationResult` into a single composite one
    (Wave E design, section 3, decision E3).

    An empty `results` (no partition needed to run - e.g. an empty input
    file list) yields a trivial `ok=True`, no-jobs-started result, matching
    the CLI backend's own no-op early return for the same input.
    """
    if not results:
        now = datetime.now(UTC)
        return OperationResult(
            ok=True,
            operation=operation,
            source=source,
            destination=destination,
            job_ids=(),
            stats=None,
            warnings=(),
            attempts=(),
            started_at=now,
            ended_at=now,
            duration=0.0,
            cancelled=False,
            error=None,
        )

    failures = [result for result in results if not result.ok]
    ok = not failures
    job_ids = tuple(job_id for result in results for job_id in result.job_ids)
    attempts = tuple(attempt for result in results for attempt in result.attempts)
    warnings = tuple(
        OperationWarning(
            message=f"{failure.source} -> {failure.destination}: {failure.error}",
            detail={
                "source": failure.source,
                "destination": failure.destination,
                "error": failure.error,
                "job_ids": failure.job_ids,
            },
        )
        for failure in failures
    )
    stats_list = [result.stats for result in results if result.stats is not None]
    stats = _sum_stats(stats_list) if stats_list else None
    started_at = min(result.started_at for result in results)
    ended_at = max(result.ended_at for result in results)
    cancelled = bool(failures) and all(failure.cancelled for failure in failures)
    error = (
        None
        if ok
        else "; ".join(
            f"{failure.source} -> {failure.destination}: {failure.error}" for failure in failures
        )
    )
    return OperationResult(
        ok=ok,
        operation=operation,
        source=source,
        destination=destination,
        job_ids=job_ids,
        stats=stats,
        warnings=warnings,
        attempts=attempts,
        started_at=started_at,
        ended_at=ended_at,
        duration=(ended_at - started_at).total_seconds(),
        cancelled=cancelled,
        error=error,
    )


def _raise_if_check_failed(check: bool, aggregate: OperationResult) -> None:
    if not check or aggregate.ok:
        return
    if aggregate.cancelled:
        raise OperationCancelledError(aggregate)
    raise OperationFailedError(aggregate)


def copy_files_embedded(
    monitor: _JobMonitor,
    client_id: uuid.UUID,
    config: Config,
    src: str,
    dst: str,
    files: list[str] | Path,
    check: bool | None = None,
    max_backlog: int | None = None,
    checkers: int | None = None,
    transfers: int | None = None,
    low_level_retries: int | None = None,
    retries: int | None = None,
    retries_sleep: str | None = None,
    metadata: bool | None = None,
    timeout: str | None = None,
    max_partition_workers: int | None = None,
    multi_thread_streams: int | None = None,
    other_args: list[str] | None = None,
) -> OperationResult:
    """Copy multiple individual files from `src` to `dst` via partitioned
    `rclonekit/copy` jobs, one per common-prefix group (Wave E design,
    decisions E1/E2/E3/E4).

    Each partition keeps the retry-aware `rclonekit/copy` endpoint (not a
    bare `sync/copy`), matching `copy()`'s own historical tuned defaults
    (checkers 1000, transfers 32, low-level retries 10, retries 3) unless
    overridden. `max_partition_workers` bounds how many partition jobs are
    outstanding (started but not yet waited-on) at once; unlike the CLI
    backend's `ThreadPoolExecutor`, no Python-side thread pool is needed -
    an RC job is already concurrent on rclone's side the moment `start()`
    returns.

    Raises `OperationFailedError`/`OperationCancelledError` only after
    every partition has been collected, never mid-collection, so a partial
    failure never loses a still-running sibling partition's result.
    """
    if other_args:
        raise UnsupportedEmbeddedOperationError("copy_files (other_args)")
    check = get_check(check)
    payload: list[str] = (
        files
        if isinstance(files, list)
        else [f.strip() for f in files.read_text().splitlines() if f.strip()]
    )
    if len(payload) == 0:
        return _aggregate_results("copy_files", src, dst, [])

    for path in payload:
        if ":" in path:
            raise ValueError(
                f"Invalid file path, contains a remote, which is not allowed for copy_files: {path}"
            )

    datalists: dict[str, list[str]] = group_files(payload, fully_qualified=False)
    partitions = list(datalists.items())
    options = TransferOptions(
        checkers=_COPY_FILES_DEFAULT_CHECKERS if checkers is None else checkers,
        transfers=_COPY_FILES_DEFAULT_TRANSFERS if transfers is None else transfers,
        low_level_retries=(
            _COPY_FILES_DEFAULT_LOW_LEVEL_RETRIES
            if low_level_retries is None
            else low_level_retries
        ),
        retries=_COPY_FILES_DEFAULT_RETRIES if retries is None else retries,
        multi_thread_streams=multi_thread_streams,
        retries_sleep=retries_sleep,
        timeout=timeout,
        max_backlog=max_backlog,
        metadata=metadata,
    )
    config_overlay = encode_transfer_options_config(options)
    batch_size = len(partitions) if max_partition_workers is None else max(1, max_partition_workers)

    results: list[OperationResult] = []
    with TemporaryDirectory() as tmpdir:
        for batch_start in range(0, len(partitions), batch_size):
            handles = []
            for offset, (common_prefix, partition_files) in enumerate(
                partitions[batch_start : batch_start + batch_size]
            ):
                src_path = f"{src}/{common_prefix}" if common_prefix else src
                dst_path = f"{dst}/{common_prefix}" if common_prefix else dst
                partition_dir = Path(tmpdir) / str(batch_start + offset)
                partition_dir.mkdir()
                filepath = write_files_from(partition_dir, partition_files)
                params: dict[str, object] = {
                    "srcFs": encode_fs_spec(config, src_path),
                    "dstFs": encode_fs_spec(config, dst_path),
                    "createEmptySrcDirs": False,
                    "_filter": {"FilesFrom": [str(filepath)]},
                }
                if config_overlay:
                    params["_config"] = config_overlay
                handle = monitor.start_job(
                    "rclonekit/copy",
                    params,
                    group=_new_group(client_id),
                    operation="copy_files",
                    source=src_path,
                    destination=dst_path,
                    check=False,
                )
                handles.append(handle)
            results.extend(handle.wait() for handle in handles)

    aggregate = _aggregate_results("copy_files", src, dst, results)
    _raise_if_check_failed(check, aggregate)
    return aggregate


def delete_files_embedded(
    monitor: _JobMonitor,
    client_id: uuid.UUID,
    config: Config,
    files: list[str],
    check: bool | None = None,
    rmdirs: bool = False,
    max_partition_workers: int | None = None,
    other_args: list[str] | None = None,
) -> OperationResult:
    """Delete multiple individual files via partitioned `operations/delete`
    jobs, one per remote/common-prefix group (Wave E design, decisions
    E1/E3/E4/E5), optionally followed by `operations/rmdirs(leaveRoot=True)`
    per partition when `rmdirs=True` - reproducing the `delete` command's
    own `--rmdirs` sequence (`cmd/delete/delete.go`), not a new one.

    A partition that fails `operations/delete` never attempts its
    `rmdirs` call - there is nothing to clean up if delete didn't finish.
    Aggregation and failure-contract semantics match `copy_files_embedded`.
    """
    if other_args:
        raise UnsupportedEmbeddedOperationError("delete_files (other_args)")
    check = get_check(check)
    if len(files) == 0:
        return _aggregate_results("delete_files", None, None, [])

    datalists: dict[str, list[str]] = group_files(list(files))
    partitions = list(datalists.items())
    batch_size = len(partitions) if max_partition_workers is None else max(1, max_partition_workers)
    config_overlay = {"Checkers": _DELETE_FILES_CHECKERS, "Transfers": _DELETE_FILES_TRANSFERS}

    results: list[OperationResult] = []
    with TemporaryDirectory() as tmpdir:
        for batch_start in range(0, len(partitions), batch_size):
            handles = []
            for offset, (remote_root, remote_files) in enumerate(
                partitions[batch_start : batch_start + batch_size]
            ):
                partition_dir = Path(tmpdir) / str(batch_start + offset)
                partition_dir.mkdir()
                filepath = write_files_from(partition_dir, remote_files)
                fs = encode_fs_spec(config, remote_root)
                handle = monitor.start_job(
                    "operations/delete",
                    {
                        "fs": fs,
                        "_filter": {"FilesFrom": [str(filepath)]},
                        "_config": config_overlay,
                    },
                    group=_new_group(client_id),
                    operation="delete_files",
                    source=remote_root,
                    destination=None,
                    check=False,
                )
                handles.append((remote_root, fs, handle))
            for remote_root, fs, handle in handles:
                delete_result = handle.wait()
                results.append(delete_result)
                if rmdirs and delete_result.ok:
                    rmdirs_handle = monitor.start_job(
                        "operations/rmdirs",
                        {"fs": fs, "remote": "", "leaveRoot": True},
                        group=_new_group(client_id),
                        operation="delete_files_rmdirs",
                        source=remote_root,
                        destination=None,
                        check=False,
                    )
                    results.append(rmdirs_handle.wait())

    aggregate = _aggregate_results("delete_files", None, None, results)
    _raise_if_check_failed(check, aggregate)
    return aggregate
