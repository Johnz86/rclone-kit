"""Curated public API for ``rclone-kit``."""

import logging

from rclone_kit.client import Rclone
from rclone_kit.completed_process import CompletedProcess
from rclone_kit.config import Config, Parsed, Section
from rclone_kit.diff import DiffItem, DiffOption, DiffType
from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.file import File, FileItem
from rclone_kit.file_stream import FilesStream
from rclone_kit.filelist import FileList
from rclone_kit.fs.filesystem import FSPath, RealFS, RemoteFS
from rclone_kit.http_server import HttpFetcher, HttpServer, Range
from rclone_kit.log import configure_logging
from rclone_kit.log import setup_default_logging as setup_default_logging
from rclone_kit.mount import Mount
from rclone_kit.process import Process
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath
from rclone_kit.s3.types import MultiUploadResult
from rclone_kit.settings import LogSettings, rclone_verbose
from rclone_kit.types import ListingOption, Order, PartInfo, SizeResult, SizeSuffix

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "CompletedProcess",
    "Config",
    "DiffItem",
    "DiffOption",
    "DiffType",
    "Dir",
    "DirListing",
    "FSPath",
    "File",
    "FileItem",
    "FileList",
    "FilesStream",
    "HttpFetcher",
    "HttpServer",
    "ListingOption",
    "LogSettings",
    "Mount",
    "MultiUploadResult",
    "Order",
    "Parsed",
    "PartInfo",
    "Process",
    "RPath",
    "Range",
    "Rclone",
    "RealFS",
    "Remote",
    "RemoteFS",
    "Section",
    "SizeResult",
    "SizeSuffix",
    "configure_logging",
    "rclone_verbose",
    "setup_default_logging",
]
