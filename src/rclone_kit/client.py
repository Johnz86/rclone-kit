"""Rclone implementation providing the public operation surface.

One difference between the directory-level transfer entry points is
load-bearing enough to state here rather than only in the methods
themselves. `copy()`/`start_copy()` run the fork's own `rclonekit/copy` RC
method, which wraps `sync.CopyDir` in the same high-level retry loop the
`rclone copy` CLI command uses and reports every attempt in the job
output - which is why `OperationResult.attempts` is populated for a copy.
There is no `rclonekit/sync` or `rclonekit/move` equivalent, so
`sync()`/`start_sync()` and `move()`/`start_move()` call upstream
`sync/sync`/`sync/move` directly: the underlying operation runs exactly
ONCE, nothing is retried at the command level, and their
`OperationResult.attempts` is always empty.

That gap is not filled in Python on purpose. rclone's retry loop resets
its accounting group's error state between attempts
(`librclone/rclonekit/rc/copy.go`), which no out-of-process caller can do
correctly; a naive Python retry would double-count stats and report
errors from an abandoned attempt as if they belonged to the successful
one. Per-file `low_level_retries` still applies to all of them - it is
enforced inside the operation, not by the command loop.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Generator
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Self

from rclone_kit.authorization import (
    AuthorizationManager,
    AuthorizationRequest,
    AuthorizationSession,
    RemoteConflictPolicy,
    Secret,
)
from rclone_kit.check import CheckResult
from rclone_kit.config import Config
from rclone_kit.convert import convert_to_filestr_list, convert_to_str
from rclone_kit.diff import DiffItem, DiffOption
from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.embedded_file_stream import EmbeddedFilesStream
from rclone_kit.exceptions import (
    OperationFailedError,
    OperationShutdownError,
    RcloneCommandError,
)
from rclone_kit.file import File
from rclone_kit.fs.filesystem import FSPath, RemoteFS
from rclone_kit.http_server import HttpServer
from rclone_kit.job import JobHandle, _JobMonitor
from rclone_kit.mount_handle import MountHandle
from rclone_kit.native.build_info import NativeBuildInfo
from rclone_kit.native.library import resolve_library_path
from rclone_kit.native.runtime import RcloneRuntime
from rclone_kit.operation import OperationResult
from rclone_kit.operations.bytes_ops import (
    read_bytes_via_temp_file,
    write_bytes_via_temp_file,
)
from rclone_kit.operations.check_ops_embedded import run_check_embedded
from rclone_kit.operations.config_ops import (
    check_is_s3,
    fetch_config_paths_embedded,
    fetch_config_show_embedded,
    fetch_s3_credentials,
)
from rclone_kit.operations.listing_ops import (
    fetch_modtime,
    fetch_modtime_dt,
    print_contents,
)
from rclone_kit.operations.listing_ops_embedded import (
    check_exists_embedded,
    check_is_synced_embedded,
    fetch_listremotes_embedded,
    fetch_ls_embedded,
    fetch_ls_stream_embedded,
    fetch_size_file_embedded,
    fetch_size_files_embedded,
    fetch_stat_embedded,
    stream_diff_embedded,
)
from rclone_kit.operations.mount_ops_embedded import fetch_mount_embedded, fetch_s3_mount_embedded
from rclone_kit.operations.s3_ops import (
    copy_file_parts_s3_resumable,
    make_s3_client,
    upload_file_s3,
)
from rclone_kit.operations.serve_ops_embedded import (
    fetch_serve_http_embedded,
    fetch_serve_webdav_embedded,
)
from rclone_kit.operations.transfer_ops_embedded import (
    cleanup_embedded,
    copy_bytes_embedded,
    copy_file_to_embedded,
    copy_files_embedded,
    delete_files_embedded,
    move_file_to_embedded,
    purge_dir_embedded,
    start_copy_files_embedded,
    start_delete_files_embedded,
    start_directory_transfer_embedded,
)
from rclone_kit.operations.transfer_options import (
    COPY_TUNED_PROFILE,
    COPY_TUNED_PROFILE_WITHOUT_RETRIES,
    TransferOptions,
)
from rclone_kit.operations.traversal_ops import scan_missing_folders_from, walk_from
from rclone_kit.optional_dependency import MissingOptionalDependencyError
from rclone_kit.partitioned_job import PartitionedJobHandle
from rclone_kit.rc.client import RcClient
from rclone_kit.rc.jobs import RcloneRcJobClient
from rclone_kit.rc.list_stream import RcloneRcListStreamClient
from rclone_kit.rc.mount import RcloneRcMountClient
from rclone_kit.rc.serve import RcloneRcServeClient
from rclone_kit.remote import Remote
from rclone_kit.serve_handle import ServeHandle
from rclone_kit.types import (
    ListingOption,
    ModTimeStrategy,
    Order,
    PartInfo,
    SizeResult,
    SizeSuffix,
)
from rclone_kit.util import get_verbose, make_temp_config_file

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from rclone_kit.rc.mount import RcMountClient
    from rclone_kit.rc.serve import RcServeClient
    from rclone_kit.s3.api import S3Client
    from rclone_kit.s3.types import S3Credentials

_DEFAULT_AUTHORIZATION_EXPIRES_IN = timedelta(minutes=10)

logger = logging.getLogger(__name__)

_JOB_SHUTDOWN_DEADLINE_SECONDS = 10.0

_CLEANUP_FAILURE_MESSAGE = "one or more tracked resources failed to close"

# RC method and `OperationResult.operation` label per directory-level
# transfer entry point.
_COPY_RC_METHOD = "rclonekit/copy"
_SYNC_RC_METHOD = "sync/sync"
_MOVE_RC_METHOD = "sync/move"
_COPY_OPERATION = "copy"
_SYNC_OPERATION = "sync"
_MOVE_OPERATION = "move"


def _to_rclone_conf(config: Config | Path | None) -> Config:
    if config is None:
        return Config(None)
    elif isinstance(config, Path):
        content = config.read_text(encoding="utf-8")
        return Config(content)
    else:
        return config


class Rclone:
    """Curated high-level API for rclone operations."""

    _job_monitor: _JobMonitor | None = None

    def __init__(
        self,
        rclone_conf: Path | Config | None,
        *,
        library_path: Path | None = None,
        runtime: RcloneRuntime | None = None,
    ) -> None:
        """Bind a config and initialize (or accept) an embedded native runtime.

        ``self.config`` is always derived from ``rclone_conf`` directly.
        Loads (or accepts) one ``RcloneRuntime`` and initializes it with an
        immutable config path derived from ``rclone_conf``. The native ABI
        permits initializing a given runtime exactly once per process - a
        caller that wants several ``Rclone`` clients to share one config
        must construct and initialize one ``RcloneRuntime`` itself and pass
        it as ``runtime`` to each client; the second client's own
        ``rclone_conf`` is then ignored, since the runtime is already
        initialized.
        """
        if isinstance(rclone_conf, Path) and not rclone_conf.exists():
            raise ValueError(f"Rclone config file not found: {rclone_conf}")
        if runtime is not None and library_path is not None:
            raise ValueError("supply at most one of runtime and library_path")

        self._embedded_runtime: RcloneRuntime
        self._rc_client: RcClient
        self._owns_embedded_runtime = False
        self._job_monitor: _JobMonitor | None = None
        self._serve_client: RcServeClient | None = None
        self._serve_handles: set[ServeHandle] = set()
        self._mount_client: RcMountClient | None = None
        self._mount_handles: set[MountHandle] = set()
        self._file_streams: set[EmbeddedFilesStream] = set()
        self._authorization_sessions: set[AuthorizationSession] = set()

        self.config = _to_rclone_conf(rclone_conf)
        self._client_id = uuid.uuid4()
        config_path = self._embedded_config_path(rclone_conf)
        if runtime is not None:
            self._embedded_runtime = runtime
        else:
            self._embedded_runtime = RcloneRuntime.from_library_path(
                resolve_library_path(library_path)
            )
            self._owns_embedded_runtime = True
        if not self._embedded_runtime.initialized:
            self._embedded_runtime.initialize(config_path=config_path)
        self._rc_client = RcClient(self._embedded_runtime)

    @staticmethod
    def _embedded_config_path(rclone_conf: Path | Config | None) -> Path | None:
        """Resolve the immutable config path passed to `RcloneRuntime.initialize`.

        `None` and an explicit `Path` are used directly. A `Config` value is
        materialized to its own temp file at most once per `Config` instance.
        """
        if rclone_conf is None:
            return None
        if isinstance(rclone_conf, Path):
            return rclone_conf
        return rclone_conf.materialize(make_temp_config_file)

    def _tracked_resource_closers(self) -> list[Callable[[], None]]:
        """Snapshot one closing callable per tracked resource, in the order
        `close()` must release them: serve and mount instances first (they
        hold VFS state on top of the runtime), then read cursors, then
        authorization sessions.

        Snapshotted up front because each callable removes its own resource
        from the set it was taken from, via the `_on_dispose`/`_on_close`
        hook `_track_*` installed.
        """
        return [
            *(handle.dispose for handle in self._serve_handles),
            *(mount_handle.dispose for mount_handle in self._mount_handles),
            *(stream.close for stream in self._file_streams),
            *(session.close for session in self._authorization_sessions),
        ]

    def _close_tracked_resources(self) -> list[Exception]:
        """Close every tracked resource, isolating failures, and return the
        ones that raised.

        Isolation is what keeps a single misbehaving resource from
        stranding the native runtime: an escaping exception here would skip
        the remaining resources, the job shutdown, and
        `RcloneRuntime.close()`, and a runtime that is never finalized
        cannot be replaced for the rest of the process's life.
        """
        errors: list[Exception] = []
        for close_resource in self._tracked_resource_closers():
            try:
                close_resource()
            except Exception as error:
                logger.exception("error releasing a tracked resource during Rclone.close()")
                errors.append(error)
        return errors

    def close(self) -> None:
        """Release resources this client owns. Idempotent.

        Cancels and waits for every job this client started (regardless of
        who owns the embedded runtime - the runtime may be injected and
        outlive this client, but jobs this client started are still its own
        responsibility) before finalizing the runtime. Raises
        `OperationShutdownError` and leaves the runtime open, rather than
        reporting a false close, if a job cannot be confirmed settled
        within the shutdown deadline. Also stops every `serve/start`
        instance, unmounts every `mount/mount` instance, closes every
        `ls_stream()` cursor, and cancels every authorization session this
        client started but never explicitly disposed: this client tracks
        only the resources it owns.

        Every one of those resource releases is failure-isolated, so one
        raising resource can never cost the others their release, the job
        shutdown, or the runtime close. Their failures are logged and then
        re-raised as an `ExceptionGroup` *after* the runtime is closed, so
        a caller is still told the close was incomplete without any
        resource being leaked to say so. An unsettled job outranks them:
        `OperationShutdownError` propagates immediately (runtime left open,
        as documented above) and the already-logged resource failures do
        not mask it.

        Only closes the embedded runtime itself if this client created it;
        an injected `runtime` outlives this client, matching
        `RcloneRuntime`'s own single-owner closing rule.
        """
        cleanup_errors = self._close_tracked_resources()
        if self._job_monitor is not None:
            all_settled = self._job_monitor.shutdown(
                deadline_seconds=_JOB_SHUTDOWN_DEADLINE_SECONDS
            )
            if not all_settled:
                raise OperationShutdownError(
                    f"one or more jobs did not settle within "
                    f"{_JOB_SHUTDOWN_DEADLINE_SECONDS}s of close(); runtime left open"
                )
        if self._owns_embedded_runtime:
            self._embedded_runtime.close()
        if cleanup_errors:
            raise ExceptionGroup(_CLEANUP_FAILURE_MESSAGE, cleanup_errors)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def native_build_info(self) -> NativeBuildInfo:
        """Report which native rclone build this embedded client links:
        ABI version, rclone version/commit, Go version, build tags, and
        target platform.
        """
        return self._embedded_runtime.build_info()

    def filesystem(self, src: str) -> RemoteFS:
        return RemoteFS(self, src)

    def cwd(self, src: str) -> FSPath:
        return self.filesystem(src).cwd()

    def obscure(self, password: str) -> str:
        """Obscure a password for use in rclone config files."""
        return self._rc_client.call("core/obscure", clear=password)["obscured"]

    def ls_stream(
        self,
        src: str,
        max_depth: int = -1,
        fast_list: bool = False,
    ) -> EmbeddedFilesStream:
        """
        List files in the given path, as a bounded-memory pull stream.

        Args:
            src: Remote path to list
            max_depth: Maximum recursion depth (-1 for unlimited)
            fast_list: Use fast list (only use when getting THE entire data repository from the root/bucket, or it's small)

        Backed by `rclonekit/liststream/*` rather than a subprocess.
        """
        stream = fetch_ls_stream_embedded(
            RcloneRcListStreamClient(self._rc_client), src, max_depth, fast_list
        )
        return self._track_file_stream(stream)

    def save_to_db(
        self,
        src: str,
        db_url: str,
        max_depth: int = -1,
        fast_list: bool = False,
    ) -> None:
        """
        Save files to a database (sqlite, mysql, postgres)

        Args:
            src: Remote path to list, this will be used to populate an entire table, so always use the root-most path.
            db_url: Database URL, like sqlite:///data.db or mysql://user:pass@localhost/db or postgres://user:pass@localhost/db
            max_depth: Maximum depth to traverse (-1 for unlimited)
            fast_list: Use fast list (only use when getting THE entire data repository from the root/bucket)

        """
        try:
            from rclone_kit.db import DB
        except ModuleNotFoundError as error:
            raise MissingOptionalDependencyError(
                "Database operations", "database", "sqlmodel"
            ) from error

        db = DB(db_url)
        with self.ls_stream(src, max_depth, fast_list) as stream:
            for page in stream.files_paged(page_size=10000):
                db.add_files(page)

    def ls(
        self,
        src: Dir | Remote | str | None = None,
        max_depth: int | None = None,
        glob: str | None = None,
        order: Order = Order.NORMAL,
        listing_option: ListingOption = ListingOption.ALL,
    ) -> DirListing:
        """List files in the given path.

        Args:
            src: Remote path or Remote object to list
            max_depth: Maximum recursion depth (0 means no recursion)

        Returns:
            List of File objects found at the path
        """
        return fetch_ls_embedded(
            self._rc_client,
            self,
            src,
            max_depth=max_depth,
            glob=glob,
            order=order,
            listing_option=listing_option,
        )

    def print(self, src: str) -> None:
        """Print the contents of a file."""
        print_contents(self, src)

    def stat(self, src: str) -> File:
        """Get the status of a file or directory.

        Raises FileNotFoundError if `src` does not exist.
        """
        return fetch_stat_embedded(self._rc_client, self, src)

    def modtime(self, src: str) -> str:
        """Get the modification time of a file or directory."""
        return fetch_modtime(self, src)

    def modtime_dt(self, src: str) -> datetime:
        """Get the modification time of a file or directory."""
        return fetch_modtime_dt(self, src)

    def listremotes(self) -> list[Remote]:
        return fetch_listremotes_embedded(self._rc_client, self)

    def diff(
        self,
        src: str,
        dst: str,
        min_size: (str | None) = None,
        max_size: (str | None) = None,
        diff_option: DiffOption = DiffOption.COMBINED,
        fast_list: bool = True,
        size_only: bool | None = None,
        checkers: int | None = None,
    ) -> Generator[DiffItem]:
        """Be extra careful with the src and dst values. If you are off by one
        parent directory, you will get a huge amount of false diffs."""
        yield from stream_diff_embedded(
            self._rc_client,
            src,
            dst,
            min_size=min_size,
            max_size=max_size,
            diff_option=diff_option,
            fast_list=fast_list,
            size_only=size_only,
            checkers=checkers,
        )

    def walk(
        self,
        src: Dir | Remote | str,
        max_depth: int = -1,
        breadth_first: bool = True,
        order: Order = Order.NORMAL,
    ) -> Generator[DirListing]:
        """Walk through the given path recursively.

        Args:
            src: Remote path or Remote object to walk through
            max_depth: Maximum depth to traverse (-1 for unlimited)

        Yields:
            DirListing: Directory listing for each directory encountered
        """
        yield from walk_from(
            self, src, max_depth=max_depth, breadth_first=breadth_first, order=order
        )

    def scan_missing_folders(
        self,
        src: Dir | Remote | str,
        dst: Dir | Remote | str,
        max_depth: int = -1,
        order: Order = Order.NORMAL,
    ) -> Generator[Dir]:
        """Yield every directory present under `src` that is missing under
        the corresponding relative path in `dst`.

        A folder found missing is yielded once for itself; if it has a
        subtree, every descendant directory is yielded too (walked via
        `detail.walk.walk_runner_depth_first`, since a whole missing
        subtree needs no further src/dst comparison - none of it exists on
        the `dst` side by definition). Folders present under `src` and
        `dst` at a given relative path are recursed into, in case they
        diverge further down.

        Args:
            src: Source directory or Remote to walk through
            dst: Destination directory or Remote to walk through
            max_depth: Maximum depth to traverse (-1 for unlimited)

        Yields:
            Dir: each directory present under `src` but missing under `dst`
        """
        yield from scan_missing_folders_from(self, src, dst, max_depth=max_depth, order=order)

    def cleanup(self, src: str) -> OperationResult:
        """Cleanup any resources used by the Rclone instance."""
        return cleanup_embedded(self._ensure_job_monitor(), self._client_id, src)

    def get_verbose(self) -> bool:
        return get_verbose(None)

    def copy_to(
        self,
        src: File | str,
        dst: File | str,
        check: bool | None = None,
    ) -> OperationResult:
        """Copy one file from source to destination.

        Warning - slow.

        """
        return copy_file_to_embedded(
            self._ensure_job_monitor(),
            self._client_id,
            self.config,
            src,
            dst,
            check=check,
        )

    def move_to(
        self,
        src: File | str,
        dst: File | str,
        check: bool | None = None,
    ) -> OperationResult:
        """Move one file from source to destination via
        `operations/movefile`, the exact counterpart of `copy_to()`.

        Destructive on the source: the file at `src` is gone once this
        returns successfully. rclone moves server-side where the backend
        supports it and falls back to copy-then-delete where it does not.

        Warning - slow.
        """
        return move_file_to_embedded(
            self._ensure_job_monitor(),
            self._client_id,
            self.config,
            src,
            dst,
            check=check,
        )

    def copy_files(
        self,
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
        """Copy multiple files from source to destination.

        Args:
            payload: Dictionary of source and destination file paths

        Returns one aggregated `OperationResult` spanning every partition.
        """
        return copy_files_embedded(
            self._ensure_job_monitor(),
            self._client_id,
            self.config,
            src,
            dst,
            files,
            check=check,
            max_backlog=max_backlog,
            checkers=checkers,
            transfers=transfers,
            low_level_retries=low_level_retries,
            retries=retries,
            retries_sleep=retries_sleep,
            metadata=metadata,
            timeout=timeout,
            max_partition_workers=max_partition_workers,
            multi_thread_streams=multi_thread_streams,
        )

    def start_copy_files(
        self,
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
        multi_thread_streams: int | None = None,
    ) -> PartitionedJobHandle:
        """Start `copy_files()`'s partitioned jobs without waiting, and
        return a non-blocking `PartitionedJobHandle` - mirroring
        `start_copy()`/`copy()`. Unlike `copy_files()`, every partition
        job is started immediately; there is no `max_partition_workers`
        pacing here.
        """
        return start_copy_files_embedded(
            self._ensure_job_monitor(),
            self._client_id,
            self.config,
            src,
            dst,
            files,
            check=check,
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

    def _ensure_job_monitor(self) -> _JobMonitor:
        if self._job_monitor is None:
            self._job_monitor = _JobMonitor(RcloneRcJobClient(self._rc_client))
        return self._job_monitor

    def _ensure_serve_client(self) -> RcServeClient:
        if self._serve_client is None:
            self._serve_client = RcloneRcServeClient(self._rc_client)
        return self._serve_client

    def _track_serve_handle(self, handle: ServeHandle) -> ServeHandle:
        """Track `handle` so `close()` disposes it if the caller never
        does - this client tracks only the resources it owns.
        `_on_dispose` removes it again as soon as it's disposed (by the
        caller or by `close()`), so a client that starts and disposes many
        short-lived serve sessions over its lifetime does not leak one
        tracked entry per session forever."""
        self._serve_handles.add(handle)
        handle._on_dispose = lambda: self._serve_handles.discard(handle)
        return handle

    def _ensure_mount_client(self) -> RcMountClient:
        if self._mount_client is None:
            self._mount_client = RcloneRcMountClient(self._rc_client)
        return self._mount_client

    def _track_mount_handle(self, handle: MountHandle) -> MountHandle:
        """Track `handle` so `close()` disposes it if the caller never
        does, mirroring `_track_serve_handle`'s rationale and its
        `_on_dispose` untracking."""
        self._mount_handles.add(handle)
        handle._on_dispose = lambda: self._mount_handles.discard(handle)
        return handle

    def authorize(
        self,
        remote_name: str,
        backend: str,
        public_callback_url: str | None = None,
        backend_options: Mapping[str, str] | None = None,
        client_id: str | None = None,
        client_secret: Secret | None = None,
        on_conflict: RemoteConflictPolicy = RemoteConflictPolicy.REJECT,
        expires_in: timedelta = _DEFAULT_AUTHORIZATION_EXPIRES_IN,
        private_listen_addr: str | None = None,
    ) -> AuthorizationSession:
        """Start authorizing a remote through rclone's own OAuth flow.

        A thin wrapper: resolves the `AuthorizationManager` shared by
        every `Rclone` client on this client's runtime
        (`AuthorizationManager.for_runtime`), so at most one authorization
        session across every client sharing that runtime is ever driving
        rclone's OAuth step at a time - see
        `docs/rclone_authorization_design.md`. The returned session is
        tracked the same way `mount()`/`serve_webdav()` track their
        handles: `close()` cancels it if this client disposes without the
        caller resolving it first.

        Leave `public_callback_url` (and `client_id`/`client_secret`)
        unset for the common local case - a script or CLI tool running on
        the same machine as the browser that completes consent - which
        works the same way plain interactive `rclone config create` does,
        no provider application registration required; see
        `AuthorizationRequest`'s docstring for why the two are linked.
        Only a relay deployment (a web service driving auth on behalf of a
        browser elsewhere) needs `public_callback_url`.
        """
        manager = AuthorizationManager.for_runtime(self._embedded_runtime)
        request = AuthorizationRequest(
            remote_name=remote_name,
            backend=backend,
            public_callback_url=public_callback_url,
            backend_options=backend_options or {},
            client_id=client_id,
            client_secret=client_secret,
            on_conflict=on_conflict,
            expires_in=expires_in,
            private_listen_addr=private_listen_addr,
        )
        session = manager.start(request, owner=str(self._client_id))
        return self._track_authorization_session(session)

    def _track_authorization_session(self, session: AuthorizationSession) -> AuthorizationSession:
        """Track `session` so `close()` cancels it if the caller never
        disposes it, mirroring `_track_serve_handle`'s rationale and its
        `_on_dispose` untracking."""
        self._authorization_sessions.add(session)
        session._on_dispose = lambda: self._authorization_sessions.discard(session)
        return session

    def _track_file_stream(self, stream: EmbeddedFilesStream) -> EmbeddedFilesStream:
        """Track `stream` so `close()` closes it if the caller never does,
        mirroring `_track_serve_handle`'s rationale and its `_on_close`
        untracking."""
        self._file_streams.add(stream)
        stream._on_close = lambda: self._file_streams.discard(stream)
        return stream

    def _start_directory_transfer(
        self,
        method: str,
        operation: str,
        src: Dir | Remote | str,
        dst: Dir | Remote | str,
        options: TransferOptions,
        *,
        check: bool | None,
        extra_params: Mapping[str, object] | None = None,
    ) -> JobHandle:
        """Bind this client's monitor and config to
        `start_directory_transfer_embedded`, the request-building body
        `start_copy()`, `start_sync()`, and `start_move()` share.
        """
        return start_directory_transfer_embedded(
            self._ensure_job_monitor(),
            self._client_id,
            self.config,
            method,
            operation,
            src,
            dst,
            options,
            check=check,
            extra_params=extra_params,
        )

    def start_copy(
        self,
        src: Dir | Remote | str,
        dst: Dir | Remote | str,
        *,
        transfers: int | None = None,
        checkers: int | None = None,
        multi_thread_streams: int | None = None,
        low_level_retries: int | None = None,
        retries: int | None = None,
        create_empty_src_dirs: bool = False,
        check: bool | None = None,
    ) -> JobHandle:
        """Start an asynchronous directory copy and return a `JobHandle`.

        Uses the retry-aware `rclonekit/copy` RC method (not a bare
        `sync/copy`), which preserves the same high-level retry loop
        `rclone copy` itself uses and records every attempt in the
        resulting `OperationResult.attempts`. `start_sync()`/`start_move()`
        have no such loop; see this module's docstring.
        """
        return self._start_directory_transfer(
            _COPY_RC_METHOD,
            _COPY_OPERATION,
            src,
            dst,
            TransferOptions(
                checkers=checkers,
                transfers=transfers,
                low_level_retries=low_level_retries,
                retries=retries,
                multi_thread_streams=multi_thread_streams,
                create_empty_src_dirs=create_empty_src_dirs,
            ),
            check=check,
        )

    def start_sync(
        self,
        src: Dir | Remote | str,
        dst: Dir | Remote | str,
        *,
        transfers: int | None = None,
        checkers: int | None = None,
        multi_thread_streams: int | None = None,
        low_level_retries: int | None = None,
        create_empty_src_dirs: bool = False,
        check: bool | None = None,
    ) -> JobHandle:
        """Start an asynchronous directory sync and return a `JobHandle`.

        DESTRUCTIVE ON THE DESTINATION: sync makes `dst` identical to
        `src`, which means deleting every file present at `dst` and absent
        at `src`. Use `copy()` for the additive variant.

        NO COMMAND-LEVEL RETRY: this calls upstream `sync/sync`, which
        runs the underlying sync exactly once - unlike `start_copy()`'s
        `rclonekit/copy`. The resulting `OperationResult.attempts` is
        always empty, and there is deliberately no `retries` parameter
        here because `_config.Retries` is read only by a command-level
        retry loop this endpoint does not have. See this module's
        docstring for why the loop is not reimplemented in Python.
        """
        return self._start_directory_transfer(
            _SYNC_RC_METHOD,
            _SYNC_OPERATION,
            src,
            dst,
            TransferOptions(
                checkers=checkers,
                transfers=transfers,
                low_level_retries=low_level_retries,
                multi_thread_streams=multi_thread_streams,
                create_empty_src_dirs=create_empty_src_dirs,
            ),
            check=check,
        )

    def start_move(
        self,
        src: Dir | Remote | str,
        dst: Dir | Remote | str,
        *,
        transfers: int | None = None,
        checkers: int | None = None,
        multi_thread_streams: int | None = None,
        low_level_retries: int | None = None,
        create_empty_src_dirs: bool = False,
        delete_empty_src_dirs: bool = False,
        check: bool | None = None,
    ) -> JobHandle:
        """Start an asynchronous directory move and return a `JobHandle`.

        DESTRUCTIVE ON THE SOURCE: every transferred file is removed from
        `src`. Files already present and identical at `dst` are still
        removed from `src`; files only at `dst` are left alone (a move is
        not a sync). `delete_empty_src_dirs` additionally removes the
        source directories left behind.

        NO COMMAND-LEVEL RETRY, exactly as for `start_sync()`: this calls
        upstream `sync/move`, so the move runs once,
        `OperationResult.attempts` is always empty, and no `retries`
        parameter is offered.
        """
        return self._start_directory_transfer(
            _MOVE_RC_METHOD,
            _MOVE_OPERATION,
            src,
            dst,
            TransferOptions(
                checkers=checkers,
                transfers=transfers,
                low_level_retries=low_level_retries,
                multi_thread_streams=multi_thread_streams,
                create_empty_src_dirs=create_empty_src_dirs,
            ),
            check=check,
            extra_params={"deleteEmptySrcDirs": delete_empty_src_dirs},
        )

    def copy(
        self,
        src: Dir | str,
        dst: Dir | str,
        check: bool | None = None,
        transfers: int | None = None,
        checkers: int | None = None,
        multi_thread_streams: int | None = None,
        low_level_retries: int | None = None,
        retries: int | None = None,
    ) -> OperationResult:
        """Copy files from source to destination.

        Each tuning parameter falls back to `copy()`'s tuned profile only
        when it is left `None`; any explicit value is passed through
        unchanged, so a caller is never silently given a different setting
        than the one they asked for.

        Args:
            src: Source directory
            dst: Destination directory
        """
        tuned = TransferOptions(
            checkers=checkers,
            transfers=transfers,
            low_level_retries=low_level_retries,
            retries=retries,
        ).with_defaults_from(COPY_TUNED_PROFILE)
        handle = self.start_copy(
            src,
            dst,
            transfers=tuned.transfers,
            checkers=tuned.checkers,
            low_level_retries=tuned.low_level_retries,
            retries=tuned.retries,
            multi_thread_streams=multi_thread_streams,
            check=check,
        )
        return handle.wait()

    def sync(
        self,
        src: Dir | str,
        dst: Dir | str,
        check: bool | None = None,
        transfers: int | None = None,
        checkers: int | None = None,
        multi_thread_streams: int | None = None,
        low_level_retries: int | None = None,
        create_empty_src_dirs: bool = False,
    ) -> OperationResult:
        """Make `dst` identical to `src`, DELETING files that exist only at
        the destination.

        This is the destructive counterpart of `copy()`. Anything under
        `dst` with no matching source path is removed; there is no undo and
        no dry-run here. Keep it behind application-level authorization and
        verify path construction (`check()`, `diff()`) before enabling it
        in a production worker.

        Runs the underlying sync exactly ONCE. `copy()` uses the fork's
        retry-aware `rclonekit/copy` endpoint and reports each attempt in
        `OperationResult.attempts`; `sync()` uses upstream `sync/sync`,
        which has no command-level retry loop, so its `attempts` is always
        empty and a transient failure is final. That is why no `retries`
        parameter is offered - see this module's docstring for why the loop
        is not reimplemented in Python. Per-file `low_level_retries` works
        identically for both.

        Each tuning parameter falls back to `copy()`'s tuned profile only
        when it is left `None`; any explicit value is passed through
        unchanged.
        """
        tuned = TransferOptions(
            checkers=checkers, transfers=transfers, low_level_retries=low_level_retries
        ).with_defaults_from(COPY_TUNED_PROFILE_WITHOUT_RETRIES)
        handle = self.start_sync(
            src,
            dst,
            transfers=tuned.transfers,
            checkers=tuned.checkers,
            low_level_retries=tuned.low_level_retries,
            multi_thread_streams=multi_thread_streams,
            create_empty_src_dirs=create_empty_src_dirs,
            check=check,
        )
        return handle.wait()

    def move(
        self,
        src: Dir | str,
        dst: Dir | str,
        check: bool | None = None,
        transfers: int | None = None,
        checkers: int | None = None,
        multi_thread_streams: int | None = None,
        low_level_retries: int | None = None,
        create_empty_src_dirs: bool = False,
        delete_empty_src_dirs: bool = False,
    ) -> OperationResult:
        """Move a directory from `src` to `dst`, DELETING the source files
        as they transfer.

        Destructive on the source, not on the destination: files that
        exist only at `dst` survive (that is `sync()`'s job, not this
        one). `delete_empty_src_dirs=True` also removes the emptied source
        directories afterwards. A failure part-way leaves some files moved
        and the rest still at the source.

        Runs the underlying move exactly ONCE, for the same reason
        `sync()` does: upstream `sync/move` has no command-level retry
        loop, so `OperationResult.attempts` is always empty and no
        `retries` parameter is offered.

        Tuning defaults match `copy()`'s tuned profile, as `sync()`'s do.
        """
        tuned = TransferOptions(
            checkers=checkers, transfers=transfers, low_level_retries=low_level_retries
        ).with_defaults_from(COPY_TUNED_PROFILE_WITHOUT_RETRIES)
        handle = self.start_move(
            src,
            dst,
            transfers=tuned.transfers,
            checkers=tuned.checkers,
            low_level_retries=tuned.low_level_retries,
            multi_thread_streams=multi_thread_streams,
            create_empty_src_dirs=create_empty_src_dirs,
            delete_empty_src_dirs=delete_empty_src_dirs,
            check=check,
        )
        return handle.wait()

    def check(
        self,
        src: Dir | Remote | str,
        dst: Dir | Remote | str,
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
        """Compare `src` and `dst` and return a typed `CheckResult` report.

        Alters neither side. `is_synced()` answers only "are these
        identical"; this returns the full per-path report behind that
        answer, so a caller can act on exactly which paths differ or are
        missing.

        Each report flag left `None` uses rclone's own documented default:
        `combined` and `match` off, `missing_on_src`, `missing_on_dst`,
        `differ`, and `error` on. `one_way` checks only that every source
        file exists at the destination; `download` compares contents
        instead of hashes, for backends that expose no usable hash.

        Unlike `copy()`/`sync()`, this is a direct synchronous RC call
        with no `JobHandle`: it cannot be cancelled or progress-polled.
        See `operations/check_ops_embedded.py`'s module docstring for why.

        A source and destination that do not match is a successful call
        reporting `success=False`, never an exception.
        """
        return run_check_embedded(
            self._rc_client,
            self.config,
            convert_to_str(src),
            convert_to_str(dst),
            one_way=one_way,
            download=download,
            combined=combined,
            missing_on_src=missing_on_src,
            missing_on_dst=missing_on_dst,
            match=match,
            differ=differ,
            error=error,
            size_only=size_only,
            fast_list=fast_list,
            checkers=checkers,
        )

    def purge(self, src: Dir | str) -> OperationResult:
        """Purge a directory"""
        return purge_dir_embedded(self._ensure_job_monitor(), self._client_id, src)

    def delete_files(
        self,
        files: str | File | list[str] | list[File],
        check: bool | None = None,
        rmdirs=False,
        max_partition_workers: int | None = None,
    ) -> OperationResult:
        """Delete a directory."""
        return delete_files_embedded(
            self._ensure_job_monitor(),
            self._client_id,
            self.config,
            convert_to_filestr_list(files),
            check=check,
            rmdirs=rmdirs,
            max_partition_workers=max_partition_workers,
        )

    def start_delete_files(
        self,
        files: str | File | list[str] | list[File],
        check: bool | None = None,
    ) -> PartitionedJobHandle:
        """Start `delete_files()`'s partitioned jobs without waiting, and
        return a non-blocking `PartitionedJobHandle` - mirroring
        `start_copy()`/`copy()`. Unlike `delete_files()`, every partition
        job is started immediately (no `max_partition_workers` pacing),
        and `rmdirs=True` is not supported - it is a sequential
        per-partition follow-up with no non-blocking equivalent; use
        `delete_files(rmdirs=True)` for that case.
        """
        return start_delete_files_embedded(
            self._ensure_job_monitor(),
            self._client_id,
            self.config,
            convert_to_filestr_list(files),
            check=check,
        )

    def exists(self, src: Dir | Remote | str | File) -> bool:
        """Check if a file or directory exists."""
        return check_exists_embedded(self._rc_client, self, convert_to_str(src))

    def is_synced(self, src: str | Dir, dst: str | Dir) -> bool:
        """Check if two directories are in sync."""
        return check_is_synced_embedded(self._rc_client, src, dst)

    def _s3_client(self, src: str, verbose: bool | None = None) -> S3Client:
        """Get an S3 client.

        Raises `MissingOptionalDependencyError` when `boto3` is absent -
        the optional-dependency boundary is inside `make_s3_client`.
        """
        return make_s3_client(self, src, verbose=verbose)

    def copy_file_s3(
        self,
        src: Path,
        dst: str,
        verbose: bool | None = None,
    ) -> None:
        """Copy a file to S3.

        Raises ValueError if `dst` is not an S3 remote.
        """
        upload_file_s3(self, src, dst, verbose=verbose)

    def is_s3(self, dst: str) -> bool:
        """Check if a remote is an S3 remote."""
        return check_is_s3(self.config, dst)

    def copy_file_s3_resumable(
        self,
        src: str,
        dst: str,
        part_infos: list[PartInfo] | None = None,
        upload_threads: int = 8,
        merge_threads: int = 4,
    ) -> None:
        """Copy parts of a file from source to destination."""
        copy_file_parts_s3_resumable(
            self,
            src,
            dst,
            part_infos=part_infos,
            upload_threads=upload_threads,
            merge_threads=merge_threads,
        )

    def write_text(
        self,
        text: str,
        dst: str,
    ) -> None:
        """Write text to a file."""
        self.write_bytes(data=text.encode("utf-8"), dst=dst)

    def write_bytes(
        self,
        data: bytes,
        dst: str,
    ) -> None:
        """Write bytes to a file.

        Raises RcloneCommandError if the underlying rclone command fails.
        """
        write_bytes_via_temp_file(self, data, dst)

    def read_bytes(self, src: str) -> bytes:
        """Read bytes from a file.

        Raises RcloneCommandError if the underlying rclone command fails
        or if rclone reports success without producing an output file.
        """
        return read_bytes_via_temp_file(self, src)

    def read_text(self, src: str) -> str:
        """Read text from a file."""
        return self.read_bytes(src).decode("utf-8")

    def size_file(self, src: str) -> SizeSuffix:
        """Get the size of a file or directory.

        Raises FileNotFoundError if no file matches `src`, or ValueError
        if more than one file matches.
        """
        return fetch_size_file_embedded(self._rc_client, self, src)

    def get_s3_credentials(self, remote: str, verbose: bool | None = None) -> S3Credentials:
        return fetch_s3_credentials(self.config, remote, verbose=verbose)

    def copy_bytes(
        self,
        src: str,
        offset: int | SizeSuffix,
        length: int | SizeSuffix,
        outfile: Path,
    ) -> None:
        """Copy a slice of bytes from the src file to outfile.

        Raises RcloneCommandError if the underlying rclone command fails.
        """
        try:
            copy_bytes_embedded(
                self._ensure_job_monitor(), self._client_id, src, offset, length, outfile
            )
        except OperationFailedError as error:
            raise RcloneCommandError("cat", error.result.error or "", error) from error

    def copy_dir(self, src: str | Dir, dst: str | Dir) -> OperationResult:
        """Copy a directory from source to destination.

        Never raises. Unlike `copy()`, uses rclone's own tuning defaults
        rather than `copy()`'s aggressive tuned profile.
        """
        return self.start_copy(src, dst, check=False).wait()

    def copy_remote(self, src: Remote, dst: Remote) -> OperationResult:
        """Copy a remote to another remote.

        Never raises. Unlike `copy()`, uses rclone's own tuning defaults
        rather than `copy()`'s aggressive tuned profile.
        """
        return self.start_copy(src, dst, check=False).wait()

    def mount(
        self,
        src: Remote | Dir | str,
        outdir: Path,
        allow_writes: bool | None = False,
        transfers: int | None = None,
        use_links: bool | None = None,
        vfs_cache_mode: str | None = None,
    ) -> MountHandle:
        """Mount a remote or directory to a local path.

        Args:
            src: Remote or directory to mount
            outdir: Local path to mount to
        """
        handle = fetch_mount_embedded(
            self._ensure_mount_client(),
            convert_to_str(src),
            outdir,
            allow_writes=allow_writes,
            transfers=transfers,
            use_links=use_links,
            vfs_cache_mode=vfs_cache_mode,
        )
        return self._track_mount_handle(handle)

    def mount_s3(
        self,
        url: str,
        outdir: Path,
        allow_writes=False,
        vfs_cache_mode="full",
        dir_cache_time: str | None = "1h",
        attribute_timeout: str | None = "1h",
        vfs_disk_space_total_size: str | None = "100M",
        transfers: int | None = 128,
        modtime_strategy: (ModTimeStrategy | None) = ModTimeStrategy.USE_SERVER_MODTIME,
        vfs_read_chunk_streams: int | None = 16,
        vfs_read_chunk_size: str | None = "4M",
        vfs_fast_fingerprint: bool = True,
        vfs_refresh: bool = True,
    ) -> MountHandle:
        """Mount a remote or directory to a local path with S3-tuned VFS
        defaults.

        Args:
            src: Remote or directory to mount
            outdir: Local path to mount to
        """
        handle = fetch_s3_mount_embedded(
            self._ensure_mount_client(),
            url,
            outdir,
            allow_writes=allow_writes,
            vfs_cache_mode=vfs_cache_mode,
            dir_cache_time=dir_cache_time,
            attribute_timeout=attribute_timeout,
            vfs_disk_space_total_size=vfs_disk_space_total_size,
            transfers=transfers,
            modtime_strategy=modtime_strategy,
            vfs_read_chunk_streams=vfs_read_chunk_streams,
            vfs_read_chunk_size=vfs_read_chunk_size,
            vfs_fast_fingerprint=vfs_fast_fingerprint,
            vfs_refresh=vfs_refresh,
        )
        return self._track_mount_handle(handle)

    def serve_webdav(
        self,
        src: Remote | Dir | str,
        user: str,
        password: str,
        addr: str = "localhost:2049",
        allow_other: bool = False,
    ) -> ServeHandle:
        """Serve a remote or directory via WebDAV.

        Args:
            src: Remote or directory to serve
            addr: Network address and port to serve on (default: localhost:2049)
            allow_other: Allow other users to access the share
        """
        handle = fetch_serve_webdav_embedded(
            self._ensure_serve_client(),
            convert_to_str(src),
            user,
            password,
            addr,
            allow_other=allow_other,
        )
        return self._track_serve_handle(handle)

    def serve_http(
        self,
        src: str,
        addr: str | None = None,
    ) -> HttpServer:
        """Serve a remote or directory via HTTP.

        Args:
            src: Remote or directory to serve
            addr: Network address and port to serve on (default: localhost:8080)
        """
        http_server = fetch_serve_http_embedded(
            self._ensure_serve_client(), src, "minimal", addr=addr
        )
        assert isinstance(http_server.process, ServeHandle)
        self._track_serve_handle(http_server.process)
        return http_server

    def config_paths(self) -> list[Path]:
        """Return the filesystem paths reported by `config/paths`: the
        config file, cache directory, and temp directory, in that fixed
        order.
        """
        return fetch_config_paths_embedded(self._rc_client)

    def config_show(self, remote: str | None = None) -> str:
        """Return the configuration text reported by `rclone config show`."""
        return fetch_config_show_embedded(self._rc_client, remote=remote)

    def size_files(
        self,
        src: str,
        files: list[str],
        fast_list: bool = False,
        check: bool | None = False,
    ) -> SizeResult:
        """Get the size of a list of files. Example of files items: "remote:bucket/to/file"."""
        return fetch_size_files_embedded(
            self._rc_client,
            self,
            src,
            files,
            fast_list=fast_list,
            check=check,
        )
