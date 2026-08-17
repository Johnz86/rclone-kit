"""Curated public API for ``rclone-kit``."""

import logging

from rclone_kit.authorization import (
    AuthorizationManager,
    AuthorizationRequest,
    AuthorizationResult,
    AuthorizationSession,
    AuthorizationStatus,
    RelayRequest,
    RelayResponse,
    RemoteConflictPolicy,
    Secret,
)
from rclone_kit.check import CheckResult
from rclone_kit.client import Rclone
from rclone_kit.config import Config, Parsed, Section
from rclone_kit.diff import DiffItem, DiffOption, DiffType
from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.embedded_file_stream import EmbeddedFilesStream
from rclone_kit.exceptions import (
    ConfigParseError,
    FilesystemError,
    HttpFetchError,
    JobExpiredError,
    JobIdentityError,
    JobRuntimeClosedError,
    MergeStateError,
    OperationCancelledError,
    OperationError,
    OperationFailedError,
    OperationShutdownError,
    OperationStartError,
    OperationTimeoutError,
    RcloneCommandError,
    RcloneKitError,
    S3MergeError,
    S3UploadError,
)
from rclone_kit.file import File, FileItem
from rclone_kit.filelist import FileList
from rclone_kit.fs.filesystem import FSPath, RealFS, RemoteFS
from rclone_kit.http_server import HttpFetcher, HttpServer
from rclone_kit.job import JobHandle
from rclone_kit.log import configure_logging
from rclone_kit.log import setup_default_logging as setup_default_logging
from rclone_kit.mount_handle import MountHandle
from rclone_kit.native.build_info import NativeBuildInfo
from rclone_kit.native.errors import NativeError
from rclone_kit.native.runtime import RcloneRuntime, shared_runtime
from rclone_kit.operation import (
    ActiveTransfer,
    JobState,
    JobStatus,
    OperationAttempt,
    OperationResult,
    OperationWarning,
    TransferStats,
)
from rclone_kit.optional_dependency import MissingOptionalDependencyError
from rclone_kit.rc.errors import RcCallError
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath
from rclone_kit.runtime.exceptions import RcloneRuntimeError
from rclone_kit.s3.types import MultiUploadResult
from rclone_kit.serve_handle import ServeHandle
from rclone_kit.settings import LogSettings
from rclone_kit.types import ListingOption, Order, PartInfo, Range, SizeResult, SizeSuffix

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "ActiveTransfer",
    "AuthorizationManager",
    "AuthorizationRequest",
    "AuthorizationResult",
    "AuthorizationSession",
    "AuthorizationStatus",
    "CheckResult",
    "Config",
    "ConfigParseError",
    "DiffItem",
    "DiffOption",
    "DiffType",
    "Dir",
    "DirListing",
    "EmbeddedFilesStream",
    "FSPath",
    "File",
    "FileItem",
    "FileList",
    "FilesystemError",
    "HttpFetchError",
    "HttpFetcher",
    "HttpServer",
    "JobExpiredError",
    "JobHandle",
    "JobIdentityError",
    "JobRuntimeClosedError",
    "JobState",
    "JobStatus",
    "ListingOption",
    "LogSettings",
    "MergeStateError",
    "MissingOptionalDependencyError",
    "MountHandle",
    "MultiUploadResult",
    "NativeBuildInfo",
    "NativeError",
    "OperationAttempt",
    "OperationCancelledError",
    "OperationError",
    "OperationFailedError",
    "OperationResult",
    "OperationShutdownError",
    "OperationStartError",
    "OperationTimeoutError",
    "OperationWarning",
    "Order",
    "Parsed",
    "PartInfo",
    "RPath",
    "Range",
    "RcCallError",
    "Rclone",
    "RcloneCommandError",
    "RcloneKitError",
    "RcloneRuntime",
    "RcloneRuntimeError",
    "RealFS",
    "RelayRequest",
    "RelayResponse",
    "Remote",
    "RemoteConflictPolicy",
    "RemoteFS",
    "S3MergeError",
    "S3UploadError",
    "Secret",
    "Section",
    "ServeHandle",
    "SizeResult",
    "SizeSuffix",
    "TransferStats",
    "configure_logging",
    "setup_default_logging",
    "shared_runtime",
]
