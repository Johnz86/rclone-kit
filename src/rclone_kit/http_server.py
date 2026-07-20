"""HTTP client for rclone's `serve http`, used to fetch file chunks for S3 multipart uploads."""

import logging
import time
import warnings
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Semaphore
from typing import Self
from urllib.parse import quote

import httpx

from rclone_kit.chunk_store import get_chunk_tmpdir
from rclone_kit.exceptions import HttpFetchError
from rclone_kit.file_part import FilePart
from rclone_kit.process import Process
from rclone_kit.s3.multipart.file_info import S3FileInfo
from rclone_kit.types import Range, SizeSuffix
from rclone_kit.util import random_str

_TIMEOUT = 10 * 60
_PUT_WARNING_EMITTED = Event()
_DOWNLOAD_RETRIES = 3
_DOWNLOAD_RETRY_DELAY_SECONDS = 10
_CHUNK_READ_SIZE = 8192 * 4

logger = logging.getLogger(__name__)

_range = range


@dataclass
class FileList:
    dirs: list[str]
    files: list[str]


_ROW_TAG = "tr"
_ROW_CLASS = "file"
_NAME_SPAN_TAG = "span"
_NAME_SPAN_CLASS = "name"
_ANCHOR_TAG = "a"
_DIRECTORY_NAME_SUFFIX = "/"


class _FileListingHTMLParser(HTMLParser):
    """Parses the directory-listing HTML produced by rclone's own
    `serve http` autoindex template.

    This depends on a fixed, self-generated HTML shape, not arbitrary web
    content: every entry is a `<tr class="file">` row containing exactly one
    `<span class="name"><a href="...">NAME</a></span>`. A name ending in `/`
    denotes a directory, otherwise a file. Any change to rclone's own
    autoindex template requires updating this parser to match.
    """

    def __init__(self) -> None:
        super().__init__()
        self.files: list[str] = []
        self.dirs: list[str] = []
        self._in_row = False
        self._in_name_span = False
        self._in_anchor = False
        self._anchor_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attribute_map = dict(attrs)
        classes = (attribute_map.get("class") or "").split()
        if tag == _ROW_TAG and _ROW_CLASS in classes:
            self._in_row = True
            return
        if self._in_row and tag == _NAME_SPAN_TAG and _NAME_SPAN_CLASS in classes:
            self._in_name_span = True
            return
        if self._in_name_span and tag == _ANCHOR_TAG:
            self._in_anchor = True
            self._anchor_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_anchor:
            self._anchor_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == _ANCHOR_TAG and self._in_anchor:
            self._in_anchor = False
            name = "".join(self._anchor_text_parts).strip()
            self._anchor_text_parts = []
            if name:
                if name.endswith(_DIRECTORY_NAME_SUFFIX):
                    self.dirs.append(name)
                else:
                    self.files.append(name)
            return
        if tag == _NAME_SPAN_TAG:
            self._in_name_span = False
            return
        if tag == _ROW_TAG:
            self._in_row = False


def _parse_files_and_dirs(html: str) -> FileList:
    parser = _FileListingHTMLParser()
    parser.feed(html)
    parser.close()
    return FileList(dirs=parser.dirs, files=parser.files)


def _concatenate_chunks(chunk_files: list[Path], dst_path: Path) -> None:
    """Append each downloaded chunk file to `dst_path` in order, removing it after."""
    if not dst_path.parent.exists():
        dst_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dst_path, "wb") as file:
        for chunk_file in chunk_files:
            logger.info(f"Appending {chunk_file} to {dst_path}")
            with open(chunk_file, "rb") as part:
                while chunk := part.read(_CHUNK_READ_SIZE):
                    file.write(chunk)
            logger.info(f"Removing {chunk_file}")
            chunk_file.unlink()


class HttpServer:
    """HTTP server configuration."""

    def __init__(self, url: str, subpath: str, process: Process) -> None:
        self.url = url
        self.subpath = subpath
        self.process: Process | None = process

    def _get_file_url(self, path: str | Path) -> str:
        escaped_path = quote(str(path).lstrip("/"), safe="/")
        return f"{self.url.rstrip('/')}/{escaped_path}"

    def _ensure_running(self) -> None:
        """Raise `RuntimeError` when this server has already been shut down."""
        if self.process is None:
            raise RuntimeError("HttpServer has already been shut down")

    def get_fetcher(self, path: str, n_threads: int = 16) -> "HttpFetcher":
        return HttpFetcher(self, path, n_threads=n_threads)

    def get(self, path: str, range: Range | None = None) -> bytes:
        """Get bytes from the server.

        Raises `HttpFetchError` on failure.
        """
        with TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir) / "download"
            self.download(path, destination, range)
            return destination.read_bytes()

    def exists(self, path: str) -> bool:
        """Check if the file exists on the server."""
        try:
            self._ensure_running()
            url = self._get_file_url(path)
            response = httpx.head(url)
            return response.status_code == httpx.codes.OK
        except Exception as e:
            warnings.warn(f"Failed to check if {self.url}/{path} exists: {e}", stacklevel=2)
            return False

    def size(self, path: str) -> int:
        """Get size of the file from the server.

        Raises `HttpFetchError` on failure.
        """
        self._ensure_running()
        url = self._get_file_url(path)
        try:
            response = httpx.head(url)
            response.raise_for_status()
            return int(response.headers["Content-Length"])
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise HttpFetchError(path, e) from e

    def put(self, path: str, data: bytes) -> None:
        """Put bytes to the server.

        Raises `HttpFetchError` on failure.
        """
        if not _PUT_WARNING_EMITTED.is_set():
            _PUT_WARNING_EMITTED.set()
            warnings.warn(
                "PUT method not implemented on the rclone binary as of 1.69", stacklevel=2
            )
        self._ensure_running()
        url = self._get_file_url(path)
        headers = {"Content-Type": "application/octet-stream"}
        try:
            response = httpx.post(url, content=data, timeout=_TIMEOUT, headers=headers)
            logger.info(f"Allowed methods: {response.headers.get('Allow')}")
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise HttpFetchError(path, e) from e

    def delete(self, path: str) -> None:
        """Remove file from the server.

        Raises `HttpFetchError` on failure.
        """
        self._ensure_running()
        url = self._get_file_url(path)
        try:
            response = httpx.delete(url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise HttpFetchError(path, e) from e

    def list(self, path: str) -> tuple[list[str], list[str]]:
        """List files on the server.

        Raises `HttpFetchError` on failure.
        """
        self._ensure_running()
        url = self.url
        if path:
            url += f"/{path}"
        url += "/?list"
        try:
            response = httpx.get(url, timeout=_TIMEOUT)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise HttpFetchError(path, e) from e
        files_and_dirs = _parse_files_and_dirs(response.content.decode())
        return files_and_dirs.files, files_and_dirs.dirs

    def download(self, path: str, dst: Path, range: Range | None = None) -> Path:
        """Get bytes from the server, retrying transient failures.

        Raises `HttpFetchError` when every attempt fails; raises
        `RuntimeError` immediately, without retrying, when the server has
        already been shut down.
        """
        self._ensure_running()

        def attempt() -> Path:
            if not dst.parent.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
            headers: dict[str, str] = {}
            if range:
                headers.update(range.to_header())
            url = self._get_file_url(path)
            try:
                with httpx.stream("GET", url, headers=headers, timeout=_TIMEOUT) as response:
                    response.raise_for_status()
                    with dst.open("wb") as file:
                        for chunk in response.iter_bytes(chunk_size=8192):
                            if chunk:
                                file.write(chunk)
                    if range:
                        length = range.end - range.start
                        logger.info(
                            f"Downloaded bytes starting at {range.start} with size {length} to {dst}"
                        )
                    else:
                        size = dst.stat().st_size
                        logger.info(f"Downloaded {size} bytes to {dst}")
                    if range is not None:
                        expected_size = (range.end - range.start).as_int()
                        actual_size = dst.stat().st_size
                        if actual_size != expected_size:
                            raise OSError(
                                f"Expected {expected_size} ranged bytes from {url}, "
                                f"received {actual_size}"
                            )
                    return dst
            except Exception as e:
                dst.unlink(missing_ok=True)
                raise HttpFetchError(path, e) from e

        last_error: HttpFetchError | None = None
        for attempt_number in _range(_DOWNLOAD_RETRIES):
            try:
                return attempt()
            except HttpFetchError as error:
                last_error = error
                warnings.warn(
                    f"Failed to download {path} to {dst}: {error}, retrying ({attempt_number})",
                    stacklevel=2,
                )
                time.sleep(_DOWNLOAD_RETRY_DELAY_SECONDS)
        assert last_error is not None
        raise last_error

    def download_multi_threaded(
        self,
        src_path: str,
        dst_path: Path,
        chunk_size: int = 32 * 1024 * 1024,
        n_threads: int = 16,
        range: Range | None = None,
    ) -> Path:
        """Copy file from src to dst, fetching chunks in parallel.

        Raises `HttpFetchError` if any chunk fails to download; downloaded
        chunk files are cleaned up before re-raising.
        """
        finished: list[Path] = []

        if range is None:
            range = Range(0, self.size(src_path))

        try:
            with ThreadPoolExecutor(max_workers=n_threads) as executor:
                futures: list[Future[Path]] = []
                start: int
                for start in _range(range.start.as_int(), range.end.as_int(), chunk_size):
                    end = min(SizeSuffix(start + chunk_size).as_int(), range.end.as_int())
                    r = Range(start=start, end=end)

                    def task(r: Range = r) -> Path:
                        dst = dst_path.with_suffix(f".{r.start}")
                        return self.download(src_path, dst, r)

                    futures.append(executor.submit(task, r))

                errors: list[Exception] = []
                for fut in futures:
                    try:
                        finished.append(fut.result())
                    except Exception as e:
                        errors.append(e)

                if errors:
                    warnings.warn(f"Failed to download chunked: {errors}", stacklevel=2)
                    raise HttpFetchError(src_path, errors[0]) from errors[0]

                _concatenate_chunks(finished, dst_path)
                return dst_path
        except Exception:
            for f in finished:
                try:
                    if f.exists():
                        f.unlink()
                except OSError as ee:
                    warnings.warn(f"Failed to delete file {f}: {ee}", stacklevel=2)
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        """Shutdown the server."""
        if self.process:
            self.process.dispose()
            self.process = None


class HttpFetcher:
    def __init__(self, server: "HttpServer", path: str, n_threads: int) -> None:
        self.server = server
        self.path = path
        self.executor = ThreadPoolExecutor(max_workers=n_threads)
        self._closed = False

        self.semaphore = Semaphore(n_threads)

    def bytes_fetcher(
        self, offset: int | SizeSuffix, size: int | SizeSuffix, extra: S3FileInfo
    ) -> Future[FilePart]:
        if isinstance(offset, SizeSuffix):
            offset = offset.as_int()
        if isinstance(size, SizeSuffix):
            size = size.as_int()

        def task() -> FilePart:
            try:
                range = Range(offset, offset + size)
                dst = get_chunk_tmpdir() / f"{random_str(12)}.chunk"
                self.server.download(self.path, dst, range)
                return FilePart(payload=dst, extra=extra)
            finally:
                self.semaphore.release()

        self.semaphore.acquire()
        fut = self.executor.submit(task)
        return fut

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.executor.shutdown(wait=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.shutdown()
