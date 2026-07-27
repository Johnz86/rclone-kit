"""Rclone implementation providing the public operation surface."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Generator
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Self

from rclone_kit.authorization import (
    AuthorizationManager,
    AuthorizationRequest,
    AuthorizationSession,
    RemoteConflictPolicy,
    Secret,
)
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
from rclone_kit.operations.config_ops import (
    check_is_s3,
    fetch_config_paths_embedded,
    fetch_config_show_embedded,
    fetch_s3_credentials,
)
from rclone_kit.operations.copy_file_parts_resumable import copy_file_parts_resumable
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
    purge_dir_embedded,
    start_copy_files_embedded,
    start_delete_files_embedded,
)
from rclone_kit.operations.transfer_options import TransferOptions, encode_transfer_options_config
from rclone_kit.operations.walk import walk
from rclone_kit.optional_dependency import MissingOptionalDependencyError
from rclone_kit.partitioned_job import PartitionedJobHandle
from rclone_kit.rc.client import RcClient
from rclone_kit.rc.fs_spec import encode_fs_spec
from rclone_kit.rc.jobs import RcloneRcJobClient
from rclone_kit.rc.list_stream import RcloneRcListStreamClient
from rclone_kit.rc.mount import RcloneRcMountClient
from rclone_kit.rc.serve import RcloneRcServeClient
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath
from rclone_kit.s3.types import S3UploadTarget
from rclone_kit.scan_missing_folders import scan_missing_folders
from rclone_kit.serve_handle import ServeHandle
from rclone_kit.types import (
    ListingOption,
    ModTimeStrategy,
    Order,
    PartInfo,
    S3PathInfo,
    SizeResult,
    SizeSuffix,
)
from rclone_kit.util import get_check, get_verbose, make_temp_config_file, to_path

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

# copy()'s aggressive tuned profile. copy_dir()/copy_remote() must not
# inherit these - they use rclone's own defaults instead.
_COPY_DEFAULT_CHECKERS = 1000
_COPY_DEFAULT_TRANSFERS = 32
_COPY_DEFAULT_LOW_LEVEL_RETRIES = 10
_COPY_DEFAULT_RETRIES = 3


def _copy_to_failure_detail(error: OperationFailedError) -> str:
    """Extract a stderr-like diagnostic string from a `copy_to()` failure."""
    return error.result.error or ""


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
        dir_obj: Dir
        if isinstance(src, Dir):
            remote = src.remote
            rpath = RPath(
                remote=remote,
                path=src.path.path,
                name=src.path.name,
                size=0,
                mime_type="inode/directory",
                mod_time="",
                is_dir=True,
            )
            rpath.set_rclone(self)
            dir_obj = Dir(rpath)
        elif isinstance(src, str):
            dir_obj = Dir(to_path(src, self))
        elif isinstance(src, Remote):
            dir_obj = Dir(src)
        else:
            raise TypeError(f"Invalid type for path: {type(src)}")

        yield from walk(dir_obj, max_depth=max_depth, breadth_first=breadth_first, order=order)

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
        src_dir = Dir(to_path(src, self))
        dst_dir = Dir(to_path(dst, self))
        yield from scan_missing_folders(src=src_dir, dst=dst_dir, max_depth=max_depth, order=order)

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

        `check` is stored on the returned handle and governs
        `JobHandle.wait()`'s raise-on-failure behavior; it is never sent to
        rclone. Uses the retry-aware `rclonekit/copy` RC method (not a bare
        `sync/copy`), which preserves the same high-level retry loop
        `rclone copy` itself uses.
        """
        check = get_check(check)
        src_str = convert_to_str(src)
        dst_str = convert_to_str(dst)
        options = TransferOptions(
            checkers=checkers,
            transfers=transfers,
            low_level_retries=low_level_retries,
            retries=retries,
            multi_thread_streams=multi_thread_streams,
            create_empty_src_dirs=create_empty_src_dirs,
        )
        params: dict[str, object] = {
            "srcFs": encode_fs_spec(self.config, src_str),
            "dstFs": encode_fs_spec(self.config, dst_str),
            "createEmptySrcDirs": create_empty_src_dirs,
        }
        config_overlay = encode_transfer_options_config(options)
        if config_overlay:
            params["_config"] = config_overlay
        monitor = self._ensure_job_monitor()
        group = f"rclone-kit/{self._client_id}/{uuid.uuid4()}"
        return monitor.start_job(
            "rclonekit/copy",
            params,
            group=group,
            operation="copy",
            source=src_str,
            destination=dst_str,
            check=check,
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
        handle = self.start_copy(
            src,
            dst,
            transfers=_COPY_DEFAULT_TRANSFERS if transfers is None else transfers,
            checkers=_COPY_DEFAULT_CHECKERS if checkers is None else checkers,
            low_level_retries=(
                _COPY_DEFAULT_LOW_LEVEL_RETRIES if low_level_retries is None else low_level_retries
            ),
            retries=_COPY_DEFAULT_RETRIES if retries is None else retries,
            multi_thread_streams=multi_thread_streams,
            check=check,
        )
        return handle.wait()

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
        """Get an S3 client."""
        try:
            from rclone_kit.s3.api import S3Client
        except ModuleNotFoundError as error:
            raise MissingOptionalDependencyError("S3 operations", "s3", "boto3") from error

        verbose = get_verbose(verbose)
        s3_creds = self.get_s3_credentials(remote=src, verbose=verbose)
        s3_client = S3Client(s3_creds=s3_creds, verbose=verbose)
        return s3_client

    def copy_file_s3(
        self,
        src: Path,
        dst: str,
        verbose: bool | None = None,
    ) -> None:
        """Copy a file to S3.

        Raises ValueError if `dst` is not an S3 remote.
        """
        if not self.is_s3(dst):
            raise ValueError(f"Destination is not an S3 remote: {dst}")
        s3_client = self._s3_client(dst, verbose=verbose)

        path_info: S3PathInfo = S3PathInfo.from_str(dst)
        target: S3UploadTarget = S3UploadTarget(
            src_file=src,
            src_file_size=src.stat().st_size,
            bucket_name=path_info.bucket,
            s3_key=path_info.key,
        )
        s3_client.upload_file(target=target)

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
        if dst.endswith("/"):
            dst = dst[:-1]
        dst_dir = f"{dst}-parts"

        copy_file_parts_resumable(
            access=self,
            src=src,
            dst_dir=dst_dir,
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
        with TemporaryDirectory() as tmpdir:
            tmpfile = Path(tmpdir) / "file.bin"
            tmpfile.write_bytes(data)
            if self.is_s3(dst):
                self.copy_file_s3(tmpfile, dst)
                return

            try:
                self.copy_to(str(tmpfile), dst, check=True)
            except OperationFailedError as error:
                raise RcloneCommandError("copyto", _copy_to_failure_detail(error), error) from error

    def read_bytes(self, src: str) -> bytes:
        """Read bytes from a file.

        Raises RcloneCommandError if the underlying rclone command fails
        or if rclone reports success without producing an output file.
        """
        with TemporaryDirectory() as tmpdir:
            tmpfile = Path(tmpdir) / "file.bin"
            try:
                self.copy_to(src, str(tmpfile), check=True)
            except OperationFailedError as error:
                raise RcloneCommandError("copyto", _copy_to_failure_detail(error), error) from error

            if not tmpfile.exists():
                raise RcloneCommandError(
                    "copyto", "", FileNotFoundError(f"{src} produced no output file")
                )
            return tmpfile.read_bytes()

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
