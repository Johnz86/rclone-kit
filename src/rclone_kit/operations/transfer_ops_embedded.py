"""Embedded RC-backed transfer operations.

`copy_file_to_embedded`/`move_file_to_embedded`/`purge_dir_embedded`/
`cleanup_embedded`
(`read_bytes`/`read_text` are transitive, since `Rclone.read_bytes` only
ever calls `self.copy_to`) each start their RC method as an asynchronous
job through the shared
`_JobMonitor` - via the same generic `_async: true` RC mechanism
`start_copy()` uses, not a bespoke Go endpoint, since
`operations/copyfile`/`operations/movefile`/`operations/purge`/
`operations/cleanup` need no retry loop of their own - then immediately
wait on the resulting handle.
This keeps a synchronous RC call from holding the runtime lock for the
whole operation, and means these functions return a real
`OperationResult`, never a synthetic `subprocess.CompletedProcess`;
`Rclone`'s own methods return that `OperationResult` directly, with no
compatibility wrapper. A failure with `check=True` raises
`OperationFailedError` (part of the execution-independent `OperationError`
hierarchy), not a raw `RcCallError`.

`copy_files_embedded`/`delete_files_embedded` are composite: each
partitions its file list via `group_files()`, starts one job per
partition (in batches of at most `max_partition_workers`, waiting for each
batch before starting the next), and folds every partition's
`OperationResult` into a single aggregate via `partitioned_job.aggregate_results()`
before optionally raising once, for the aggregate as a whole.
`start_copy_files_embedded()`/`start_delete_files_embedded()` are their
non-blocking counterparts: every partition job is started immediately (no
`max_partition_workers` batching - a fire-and-forget entry point has no
notion of "outstanding" jobs to pace), returning a `PartitionedJobHandle`
a caller can `.watch()`/`.wait()` like a plain `JobHandle`.
`start_delete_files_embedded()` does not support `rmdirs=True`; see its
own docstring for why.

`copy_bytes_embedded` starts the downstream `rclonekit/readrange` endpoint
as a job too, even though a byte range is bounded (not partitioned like
`copy_files_embedded`/`delete_files_embedded`) - purely so a large
in-flight range download can be observed/cancelled/timed-out like any
other embedded operation, instead of holding the runtime lock
synchronously for the whole transfer.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from rclone_kit.convert import convert_to_str
from rclone_kit.group_files import group_files
from rclone_kit.operation import OperationResult
from rclone_kit.operations.transfer_options import TransferOptions, encode_transfer_options_config
from rclone_kit.partitioned_job import (
    PartitionedJobHandle,
    aggregate_results,
    raise_if_check_failed,
)
from rclone_kit.rc.fs_spec import encode_fs_spec
from rclone_kit.rc.paths import RcPath
from rclone_kit.types import SizeSuffix
from rclone_kit.util import get_check, write_files_from

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rclone_kit.config import Config
    from rclone_kit.dir import Dir
    from rclone_kit.file import File
    from rclone_kit.job import JobHandle, _JobMonitor

_COPY_FILE_METHOD = "operations/copyfile"
_MOVE_FILE_METHOD = "operations/movefile"
_COPY_TO_OPERATION = "copy_to"
_MOVE_TO_OPERATION = "move_to"

_COPY_FILES_DEFAULT_CHECKERS = 1000
_COPY_FILES_DEFAULT_TRANSFERS = 32
_COPY_FILES_DEFAULT_LOW_LEVEL_RETRIES = 10
_COPY_FILES_DEFAULT_RETRIES = 3
_DELETE_FILES_CHECKERS = 1000
_DELETE_FILES_TRANSFERS = 1000


def _new_group(client_id: uuid.UUID) -> str:
    return f"rclone-kit/{client_id}/{uuid.uuid4()}"


def _single_file_transfer_embedded(
    monitor: _JobMonitor,
    client_id: uuid.UUID,
    config: Config,
    src: File | str,
    dst: File | str,
    *,
    method: str,
    operation: str,
    check: bool | None,
) -> OperationResult:
    """Run one `srcFs`/`srcRemote`/`dstFs`/`dstRemote` RC method as a job
    and wait for it - the whole body `copy_file_to_embedded` and
    `move_file_to_embedded` share, which differ only by RC method name and
    result label.

    Both sides split at their final path component: `operations/copyfile`
    and `operations/movefile` need `srcFs`/`dstFs` to be navigable
    directory roots, never the bare file paths themselves. Each side's
    resulting `fs` value is then passed through `encode_fs_spec`, so a
    configured S3/B2 remote gets rclone's RC config-object form
    (`_name`/`_root`/`no_check_bucket`) instead of a plain string.
    """
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
        method,
        params,
        group=_new_group(client_id),
        operation=operation,
        source=src_str,
        destination=dst_str,
        check=get_check(check),
    )
    return handle.wait()


def copy_file_to_embedded(
    monitor: _JobMonitor,
    client_id: uuid.UUID,
    config: Config,
    src: File | str,
    dst: File | str,
    check: bool | None = None,
) -> OperationResult:
    """Copy one file from source to destination via `operations/copyfile`.

    Raises `OperationFailedError` when `check` resolves `True` (the
    default) and the job fails; otherwise returns an `OperationResult`
    whose `.ok` reflects success.
    """
    return _single_file_transfer_embedded(
        monitor,
        client_id,
        config,
        src,
        dst,
        method=_COPY_FILE_METHOD,
        operation=_COPY_TO_OPERATION,
        check=check,
    )


def move_file_to_embedded(
    monitor: _JobMonitor,
    client_id: uuid.UUID,
    config: Config,
    src: File | str,
    dst: File | str,
    check: bool | None = None,
) -> OperationResult:
    """Move one file from source to destination via `operations/movefile`.

    Destructive on the source: the source file is gone once this returns
    successfully. rclone performs a server-side move where the backend
    supports one and falls back to copy-then-delete where it does not, so
    a failure part-way can leave the file present on both sides - never on
    neither.

    Failure contract is `copy_file_to_embedded`'s exactly: raises
    `OperationFailedError` when `check` resolves `True` (the default) and
    the job fails, otherwise returns an `OperationResult` whose `.ok`
    reflects success.
    """
    return _single_file_transfer_embedded(
        monitor,
        client_id,
        config,
        src,
        dst,
        method=_MOVE_FILE_METHOD,
        operation=_MOVE_TO_OPERATION,
        check=check,
    )


def purge_dir_embedded(
    monitor: _JobMonitor, client_id: uuid.UUID, src: str | Dir
) -> OperationResult:
    """Purge a directory and all of its contents via `operations/purge`.

    Never raises: always returns a result for the caller to inspect via
    `.ok`.
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

    Never raises. `src` is reassembled through `RcPath.parse` (rather than
    forwarded as a raw string) so a bare local reference gets absolutized -
    see `RcPath`/`_resolve_local` for why this matters for the shared
    embedded runtime; a configured or inline remote passes through
    unchanged.
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


def _read_copy_files_list(files: list[str] | Path) -> list[str]:
    payload: list[str] = (
        files
        if isinstance(files, list)
        else [f.strip() for f in files.read_text().splitlines() if f.strip()]
    )
    for path in payload:
        if ":" in path:
            raise ValueError(
                f"Invalid file path, contains a remote, which is not allowed for copy_files: {path}"
            )
    return payload


def _copy_files_partitions(
    payload: list[str],
    *,
    max_backlog: int | None,
    checkers: int | None,
    transfers: int | None,
    low_level_retries: int | None,
    retries: int | None,
    retries_sleep: str | None,
    metadata: bool | None,
    timeout: str | None,
    multi_thread_streams: int | None,
) -> tuple[list[tuple[str, list[str]]], Mapping[str, object]]:
    datalists: dict[str, list[str]] = group_files(payload, fully_qualified=False)
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
    return list(datalists.items()), encode_transfer_options_config(options)


def _start_copy_partition(
    monitor: _JobMonitor,
    client_id: uuid.UUID,
    config: Config,
    tmpdir: Path,
    src_path: str,
    dst_path: str,
    partition_files: list[str],
    config_overlay: Mapping[str, object],
) -> JobHandle:
    partition_dir = tmpdir / uuid.uuid4().hex
    partition_dir.mkdir()
    filepath = write_files_from(partition_dir, partition_files)
    params: dict[str, object] = {
        "srcFs": encode_fs_spec(config, src_path),
        "dstFs": encode_fs_spec(config, dst_path),
        "createEmptySrcDirs": False,
        "_filter": {"FilesFrom": [str(filepath)]},
    }
    if config_overlay:
        params["_config"] = dict(config_overlay)
    return monitor.start_job(
        "rclonekit/copy",
        params,
        group=_new_group(client_id),
        operation="copy_files",
        source=src_path,
        destination=dst_path,
        check=False,
    )


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
) -> OperationResult:
    """Copy multiple individual files from `src` to `dst` via partitioned
    `rclonekit/copy` jobs, one per common-prefix group.

    Each partition keeps the retry-aware `rclonekit/copy` endpoint (not a
    bare `sync/copy`), matching `copy()`'s own tuned defaults (checkers
    1000, transfers 32, low-level retries 10, retries 3) unless overridden.
    `max_partition_workers` bounds how many partition jobs are
    outstanding (started but not yet waited-on) at once - an RC job is
    already concurrent on rclone's side the moment `start()` returns, so no
    Python-side thread pool is needed.

    Raises `OperationFailedError`/`OperationCancelledError` only after
    every partition has been collected, never mid-collection, so a partial
    failure never loses a still-running sibling partition's result.
    """
    check = get_check(check)
    payload = _read_copy_files_list(files)
    if len(payload) == 0:
        return aggregate_results("copy_files", src, dst, [])

    partitions, config_overlay = _copy_files_partitions(
        payload,
        max_backlog=max_backlog,
        checkers=checkers,
        transfers=transfers,
        low_level_retries=low_level_retries,
        retries=retries,
        retries_sleep=retries_sleep,
        metadata=metadata,
        timeout=timeout,
        multi_thread_streams=multi_thread_streams,
    )
    batch_size = len(partitions) if max_partition_workers is None else max(1, max_partition_workers)

    results: list[OperationResult] = []
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for batch_start in range(0, len(partitions), batch_size):
            handles = [
                _start_copy_partition(
                    monitor,
                    client_id,
                    config,
                    tmpdir_path,
                    f"{src}/{common_prefix}" if common_prefix else src,
                    f"{dst}/{common_prefix}" if common_prefix else dst,
                    partition_files,
                    config_overlay,
                )
                for common_prefix, partition_files in partitions[
                    batch_start : batch_start + batch_size
                ]
            ]
            results.extend(handle.wait() for handle in handles)

    aggregate = aggregate_results("copy_files", src, dst, results)
    raise_if_check_failed(check, aggregate)
    return aggregate


def start_copy_files_embedded(
    monitor: _JobMonitor,
    client_id: uuid.UUID,
    config: Config,
    src: str,
    dst: str,
    files: list[str] | Path,
    *,
    check: bool | None = None,
    max_backlog: int | None = None,
    checkers: int | None = None,
    transfers: int | None = None,
    low_level_retries: int | None = None,
    retries: int | None = None,
    retries_sleep: str | None = None,
    metadata: bool | None = None,
    timeout: str | None = None,
    multi_thread_streams: int | None = None,
) -> PartitionedJobHandle:
    """Start every `copy_files()` partition job immediately and return a
    non-blocking `PartitionedJobHandle` - unlike `copy_files_embedded`'s
    own `max_partition_workers` batching, every partition job is started
    up front; pacing outstanding jobs is only available through the
    blocking `copy_files()` wrapper.

    The partitions' `--files-from` lists live in a directory created with
    `tempfile.mkdtemp` (not a `with TemporaryDirectory()` block, since the
    jobs reading it are still running when this function returns); it is
    removed once `PartitionedJobHandle.wait()` observes every partition
    settled.
    """
    check = get_check(check)
    payload = _read_copy_files_list(files)
    if len(payload) == 0:
        return PartitionedJobHandle(
            (), operation="copy_files", source=src, destination=dst, check=check
        )

    partitions, config_overlay = _copy_files_partitions(
        payload,
        max_backlog=max_backlog,
        checkers=checkers,
        transfers=transfers,
        low_level_retries=low_level_retries,
        retries=retries,
        retries_sleep=retries_sleep,
        metadata=metadata,
        timeout=timeout,
        multi_thread_streams=multi_thread_streams,
    )
    tmpdir = Path(tempfile.mkdtemp(prefix="rclone-kit-copy-files-"))
    handles = [
        _start_copy_partition(
            monitor,
            client_id,
            config,
            tmpdir,
            f"{src}/{common_prefix}" if common_prefix else src,
            f"{dst}/{common_prefix}" if common_prefix else dst,
            partition_files,
            config_overlay,
        )
        for common_prefix, partition_files in partitions
    ]
    return PartitionedJobHandle(
        handles,
        operation="copy_files",
        source=src,
        destination=dst,
        check=check,
        cleanup=lambda: shutil.rmtree(tmpdir, ignore_errors=True),
    )


def _start_delete_partition(
    monitor: _JobMonitor,
    client_id: uuid.UUID,
    config: Config,
    tmpdir: Path,
    remote_root: str,
    remote_files: list[str],
    config_overlay: Mapping[str, object],
) -> JobHandle:
    partition_dir = tmpdir / uuid.uuid4().hex
    partition_dir.mkdir()
    filepath = write_files_from(partition_dir, remote_files)
    fs = encode_fs_spec(config, remote_root)
    return monitor.start_job(
        "operations/delete",
        {"fs": fs, "_filter": {"FilesFrom": [str(filepath)]}, "_config": dict(config_overlay)},
        group=_new_group(client_id),
        operation="delete_files",
        source=remote_root,
        destination=None,
        check=False,
    )


def delete_files_embedded(
    monitor: _JobMonitor,
    client_id: uuid.UUID,
    config: Config,
    files: list[str],
    check: bool | None = None,
    rmdirs: bool = False,
    max_partition_workers: int | None = None,
) -> OperationResult:
    """Delete multiple individual files via partitioned `operations/delete`
    jobs, one per remote/common-prefix group, optionally followed by
    `operations/rmdirs(leaveRoot=True)` per partition when `rmdirs=True` -
    reproducing the `delete` command's own `--rmdirs` sequence
    (`cmd/delete/delete.go`).

    A partition that fails `operations/delete` never attempts its
    `rmdirs` call - there is nothing to clean up if delete didn't finish.
    Aggregation and failure-contract semantics match `copy_files_embedded`.
    """
    check = get_check(check)
    if len(files) == 0:
        return aggregate_results("delete_files", None, None, [])

    datalists: dict[str, list[str]] = group_files(list(files))
    partitions = list(datalists.items())
    batch_size = len(partitions) if max_partition_workers is None else max(1, max_partition_workers)
    config_overlay = {"Checkers": _DELETE_FILES_CHECKERS, "Transfers": _DELETE_FILES_TRANSFERS}

    results: list[OperationResult] = []
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        for batch_start in range(0, len(partitions), batch_size):
            batch = partitions[batch_start : batch_start + batch_size]
            handles = [
                (
                    remote_root,
                    _start_delete_partition(
                        monitor,
                        client_id,
                        config,
                        tmpdir_path,
                        remote_root,
                        remote_files,
                        config_overlay,
                    ),
                )
                for remote_root, remote_files in batch
            ]
            for remote_root, handle in handles:
                delete_result = handle.wait()
                results.append(delete_result)
                if rmdirs and delete_result.ok:
                    rmdirs_handle = monitor.start_job(
                        "operations/rmdirs",
                        {
                            "fs": encode_fs_spec(config, remote_root),
                            "remote": "",
                            "leaveRoot": True,
                        },
                        group=_new_group(client_id),
                        operation="delete_files_rmdirs",
                        source=remote_root,
                        destination=None,
                        check=False,
                    )
                    results.append(rmdirs_handle.wait())

    aggregate = aggregate_results("delete_files", None, None, results)
    raise_if_check_failed(check, aggregate)
    return aggregate


def start_delete_files_embedded(
    monitor: _JobMonitor,
    client_id: uuid.UUID,
    config: Config,
    files: list[str],
    *,
    check: bool | None = None,
    rmdirs: bool = False,
) -> PartitionedJobHandle:
    """Start every `delete_files()` partition job immediately and return a
    non-blocking `PartitionedJobHandle` - unlike `delete_files_embedded`'s
    own `max_partition_workers` batching, every partition job is started
    up front.

    `rmdirs=True` is not supported here and raises `ValueError`:
    `delete_files_embedded`'s `rmdirs` step is a sequential per-partition
    follow-up that only starts once *that* partition's delete has been
    observed to succeed - there is no non-blocking equivalent without a
    background orchestrator this "start everything, aggregate
    independently" handle deliberately does not have. Use
    `delete_files(rmdirs=True)` (the blocking wrapper) for that case.
    """
    if rmdirs:
        raise ValueError(
            "start_delete_files() does not support rmdirs=True; "
            "use delete_files(rmdirs=True) instead"
        )
    check = get_check(check)
    if len(files) == 0:
        return PartitionedJobHandle(
            (), operation="delete_files", source=None, destination=None, check=check
        )

    datalists: dict[str, list[str]] = group_files(list(files))
    partitions = list(datalists.items())
    config_overlay = {"Checkers": _DELETE_FILES_CHECKERS, "Transfers": _DELETE_FILES_TRANSFERS}

    tmpdir = Path(tempfile.mkdtemp(prefix="rclone-kit-delete-files-"))
    handles = [
        _start_delete_partition(
            monitor, client_id, config, tmpdir, remote_root, remote_files, config_overlay
        )
        for remote_root, remote_files in partitions
    ]
    return PartitionedJobHandle(
        handles,
        operation="delete_files",
        source=None,
        destination=None,
        check=check,
        cleanup=lambda: shutil.rmtree(tmpdir, ignore_errors=True),
    )


def copy_bytes_embedded(
    monitor: _JobMonitor,
    client_id: uuid.UUID,
    src: str,
    offset: int | SizeSuffix,
    length: int | SizeSuffix,
    outfile: Path,
) -> OperationResult:
    """Copy a byte range from `src` into `outfile` via the downstream
    `rclonekit/readrange` RC method.

    Always raises `OperationFailedError` on failure (`check` is not
    exposed here - `copy_bytes()` always runs with an effective
    `check=True`).
    """
    target = RcPath.parse(src).as_parent_and_name()
    offset_int = SizeSuffix(offset).as_int()
    length_int = SizeSuffix(length).as_int()
    handle = monitor.start_job(
        "rclonekit/readrange",
        {
            "fs": target.fs,
            "remote": target.remote,
            "offset": offset_int,
            "count": length_int,
            "outputPath": str(outfile),
        },
        group=_new_group(client_id),
        operation="copy_bytes",
        source=src,
        destination=str(outfile),
        check=True,
    )
    return handle.wait()
