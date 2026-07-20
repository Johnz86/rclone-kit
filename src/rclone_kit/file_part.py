import atexit
import logging
import warnings
from pathlib import Path
from threading import Lock

from rclone_kit.chunk_store import get_chunk_tmpdir
from rclone_kit.s3.multipart.file_info import S3FileInfo
from rclone_kit.types import SizeSuffix
from rclone_kit.util import random_str

logger = logging.getLogger(__name__)

_CLEANUP_LIST: set[Path] = set()
_exit_cleanup_lock = Lock()


def _add_for_cleanup(path: Path) -> None:
    _register_exit_cleanup_handlers()
    _CLEANUP_LIST.add(path)


def _remove_from_cleanup(path: Path) -> None:
    _CLEANUP_LIST.discard(path)


def _on_exit_cleanup() -> None:
    paths = list(_CLEANUP_LIST)
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except Exception as e:
            warnings.warn(f"Cannot cleanup {path}: {e}", stacklevel=2)


def _register_exit_cleanup_handlers() -> None:
    """Register this module's `atexit` handler, once, the first time a
    chunk file is staged for cleanup.

    Called from `_add_for_cleanup` - the sole producer of `_CLEANUP_LIST`
    - rather than at import time, so a process that merely imports
    `rclone_kit` without ever constructing a `FilePart` backed by a chunk
    file (HTTP range fetch, S3 multipart) never wires up this handler.
    Guarded by `_exit_cleanup_lock` and a function-attribute flag (the same
    lock-plus-flag idiom `chunk_store.get_chunk_tmpdir` uses for its own
    first-use guard) so constructing many `FilePart` instances, including
    concurrently across threads, only ever triggers one `atexit.register`
    call.
    """
    with _exit_cleanup_lock:
        state = _register_exit_cleanup_handlers.__dict__
        if state.get("registered"):
            return
        atexit.register(_on_exit_cleanup)
        state["registered"] = True


class FilePart:
    def __init__(self, payload: Path | bytes | Exception, extra: S3FileInfo) -> None:
        self.extra = extra
        self._lock = Lock()
        self._disposed = False
        self.payload: Path | Exception
        if isinstance(payload, Exception):
            self.payload = payload
            return
        if isinstance(payload, bytes):
            logger.debug("Creating file part with payload: %d bytes", len(payload))
            self.payload = get_chunk_tmpdir() / f"{random_str(12)}.chunk"
            self.payload.write_bytes(payload)
            _add_for_cleanup(self.payload)
        if isinstance(payload, Path):
            logger.debug("Adopting payload: %s", payload)
            self.payload = payload
            _add_for_cleanup(self.payload)

    def get_file(self) -> Path:
        """Return the successfully-fetched chunk file.

        Raises the original fetch/read failure if this part represents an
        error rather than a successful payload.
        """
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    @property
    def size(self) -> int:
        with self._lock:
            if isinstance(self.payload, Path):
                return self.payload.stat().st_size
            return -1

    def n_bytes(self) -> int:
        return self.size

    def load(self) -> bytes:
        with self._lock:
            if isinstance(self.payload, Path):
                with open(self.payload, "rb") as f:
                    return f.read()
            raise ValueError("Cannot load from error")

    def is_error(self) -> bool:
        return isinstance(self.payload, Exception)

    def dispose(self) -> None:
        with self._lock:
            if self._disposed:
                return
            self._disposed = True
            logger.debug("Disposing file part")
            if isinstance(self.payload, Exception):
                warnings.warn(
                    f"Cannot close file part because the payload represents an error: {self.payload}",
                    stacklevel=2,
                )
                return
            if self.payload.exists():
                try:
                    self.payload.unlink()
                    logger.debug("File part %s deleted", self.payload)
                except Exception as e:
                    warnings.warn(f"Cannot close file part because of error: {e}", stacklevel=2)
            else:
                warnings.warn(
                    f"Cannot close file part because it does not exist: {self.payload}",
                    stacklevel=2,
                )
            _remove_from_cleanup(self.payload)

    def __del__(self):
        self.dispose()

    def __repr__(self):
        payload_str = "err" if self.is_error() else f"{SizeSuffix(self.n_bytes())}"
        return f"FilePart(payload={payload_str}, extra={self.extra})"
