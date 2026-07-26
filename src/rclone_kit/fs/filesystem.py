import abc
import logging
import shutil
import stat
import warnings
from collections.abc import Generator
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING, Protocol, Self

from rclone_kit.exceptions import FilesystemError
from rclone_kit.fs.walk import fs_walk
from rclone_kit.fs.walk_threaded_walker import FSWalker
from rclone_kit.http_server import HttpServer
from rclone_kit.operation import OperationResult

if TYPE_CHECKING:
    from rclone_kit.dir_listing import DirListing
    from rclone_kit.file import File
    from rclone_kit.remote import Remote


class RemoteFSAccess(Protocol):
    """High-level capabilities used by the remote filesystem adapter.

    `exists`/`stat`/`ls` back `RemoteFS`'s own `exists`/`is_dir`/`is_file`/
    `ls` directly via RC (CLI-to-C-ABI migration Wave G design, F03) -
    `serve_http` is no longer called by `RemoteFS` construction (F01), only
    by its own explicit, on-demand `serve()` method.
    """

    def serve_http(
        self,
        src: str,
        addr: str | None = None,
    ) -> HttpServer: ...

    def is_s3(self, dst: str) -> bool: ...

    def copy_file_s3(self, src: Path, dst: str, verbose: bool | None = None) -> None: ...

    def copy_to(self, src: str, dst: str) -> OperationResult: ...

    def read_bytes(self, src: str) -> bytes: ...

    def write_bytes(self, data: bytes, dst: str) -> None: ...

    def delete_files(self, files: str) -> OperationResult: ...

    def exists(self, src: "Remote | str | File") -> bool: ...

    def stat(self, src: str) -> "File": ...

    def ls(self, src: str, max_depth: int | None = None) -> "DirListing": ...


logger = logging.getLogger(__name__)


class FS(abc.ABC):
    @abc.abstractmethod
    def copy(self, src: Path | str, dst: Path | str) -> None:
        pass

    @abc.abstractmethod
    def read_bytes(self, path: Path | str) -> bytes:
        pass

    @abc.abstractmethod
    def exists(self, path: Path | str) -> bool:
        pass

    @abc.abstractmethod
    def write_binary(self, path: Path | str, data: bytes) -> None:
        pass

    @abc.abstractmethod
    def mkdir(self, path: str, parents=True, exist_ok=True) -> None:
        pass

    @abc.abstractmethod
    def ls(self, path: Path | str) -> tuple[list[str], list[str]]:
        """List the immediate children of `path`. Returns `(files, dirs)`.

        No ordering guarantee is made; callers that need a stable order
        must sort explicitly (`RealFS.ls` orders however `Path.iterdir()`
        does, which is filesystem-dependent).

        The two entries in `(files, dirs)` are NOT the same shape across
        implementations, and callers must not assume otherwise:

        - `RealFS.ls` returns full path strings (`path` joined with each
          child's name), because it builds them from `Path.iterdir()`.
        - `RemoteFS.ls` returns bare name strings relative to `path`
          (no `path` prefix), and a directory's name keeps its trailing
          `/` marker from the rclone HTTP autoindex listing; a file's name
          never has one.

        Despite the difference, `FSPath.__truediv__` (`self / name`) joins
        either result back into a correct child `FSPath`: `pathlib`'s `/`
        operator discards the left operand when the right operand is
        itself an absolute path, which is exactly what makes `RealFS`'s
        full-path entries round-trip correctly through `current / name`
        instead of doubling the prefix. This is why `fs_walk`
        (`fs/walk.py`) and `FSPath.lspaths()` can call `current_dir / name`
        uniformly for both `FS` implementations without branching on type.
        Do not change `RealFS.ls` to return bare names without also
        auditing every `current / name` call site - the round-trip
        currently depends on this asymmetry, not despite it.

        Raises `FileNotFoundError` when `path` does not exist
        (`RemoteFS.ls`); `RealFS.ls` raises whatever `Path.iterdir()`
        raises for a missing path (`FileNotFoundError` as well, via the
        stdlib).
        """

    @abc.abstractmethod
    def remove(self, path: Path | str) -> None:
        """Remove a file or symbolic link."""

    @abc.abstractmethod
    def unlink(self, path: Path | str) -> None:
        """Remove a file or symbolic link."""

    @abc.abstractmethod
    def cwd(self) -> "FSPath":
        pass

    @abc.abstractmethod
    def get_path(self, path: str) -> "FSPath":
        pass

    @abc.abstractmethod
    def dispose(self) -> None:
        pass


class RealFS(FS):
    @staticmethod
    def from_path(path: Path | str) -> "FSPath":
        path_str = Path(path).as_posix()
        return FSPath(RealFS(), path_str)

    def __init__(self) -> None:
        super().__init__()

    def ls(self, path: Path | str) -> tuple[list[str], list[str]]:
        files: list[str] = []
        dirs: list[str] = []
        for entry in Path(path).iterdir():
            try:
                mode = entry.stat().st_mode
            except OSError:
                continue
            if stat.S_ISREG(mode):
                files.append(str(entry))
            elif stat.S_ISDIR(mode):
                dirs.append(str(entry))
        return files, dirs

    def cwd(self) -> "FSPath":
        return RealFS.from_path(Path.cwd())

    def copy(self, src: Path | str, dst: Path | str) -> None:
        shutil.copy(str(src), str(dst))

    def read_bytes(self, path: Path | str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    def write_binary(self, path: Path | str, data: bytes) -> None:
        with open(path, "wb") as f:
            f.write(data)

    def exists(self, path: Path | str) -> bool:
        return Path(path).exists()

    def unlink(self, path: Path | str) -> None:
        """Remove a file or symbolic link."""
        Path(path).unlink()

    def remove(self, path: Path | str, ignore_errors=False) -> None:
        """Remove a file or directory."""
        path = Path(path)
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=ignore_errors)
            else:
                path.unlink()
        except KeyboardInterrupt:
            raise
        except FileNotFoundError:
            raise
        except OSError as e:
            raise FilesystemError(str(path), e) from e

    def mkdir(self, path: str, parents=True, exist_ok=True) -> None:
        Path(path).mkdir(parents=parents, exist_ok=exist_ok)

    def get_path(self, path: str) -> "FSPath":
        return FSPath(self, path)

    def dispose(self) -> None:
        pass


class RemoteFS(FS):
    """A direct-RC filesystem facade over one rclone client (CLI-to-C-ABI
    migration Wave G design). Construction binds no port and starts no
    server (F01): `exists`/`is_dir`/`is_file`/`ls` go straight to
    `operations/stat`/`operations/list` via `rclone.exists()`/`stat()`/
    `ls()` (F03), which already dispatch correctly for both CLI and
    embedded clients. `read_bytes`/`write_binary`/`copy`/`remove` were
    already transitive through `rclone`'s own public methods (F04) and are
    unchanged here.

    An HTTP server is created only on explicit, on-demand request via
    `serve()` - for a future consumer that genuinely needs a real listener
    (e.g. multipart/resumable downloads, Wave H) - never implicitly.
    """

    def __init__(self, rclone: RemoteFSAccess, src: str) -> None:
        super().__init__()
        self.src = src
        self.shutdown = False
        self.server: HttpServer | None = None
        self.rclone = rclone

    def serve(self, addr: str | None = None) -> HttpServer:
        """Start (or return the already-started) HTTP server for this
        filesystem's root. Not needed for `exists`/`is_dir`/`is_file`/
        `ls`/`read_bytes`/`write_binary`/`copy`/`remove` - only for a
        consumer that explicitly needs a real `HttpServer`."""
        if self.server is None:
            self.server = self.rclone.serve_http(src=self.src, addr=addr)
        return self.server

    def root(self) -> "FSPath":
        return FSPath(self, self.src)

    def cwd(self) -> "FSPath":
        return self.root()

    def _to_str(self, path: Path | str) -> str:
        if isinstance(path, Path):
            return path.as_posix()
        return path

    def _to_remote_path(self, path: str | Path) -> str:
        """Make `path` relative to `self.src`.

        Uses `PurePosixPath`, not `Path`: `path` is always a
        forward-slash-delimited rclone remote path here, never a local
        filesystem path, so it must not be parsed with `WindowsPath`
        semantics (which treats a literal `\\` in a path segment - a valid
        character in many remote object keys - as a separator).
        """
        return str(PurePosixPath(path).relative_to(self.src))

    def copy(self, src: Path | str, dst: Path | str) -> None:
        src = src if isinstance(src, Path) else Path(src)

        dst_remote_path = self._to_remote_path(dst)

        is_s3 = self.rclone.is_s3(dst_remote_path)
        if is_s3:
            filesize = src.stat().st_size
            if filesize < 1024 * 1024 * 1024:
                logger.info(f"S3 OPTIMIZED: Copying {src} -> {dst_remote_path}")
                try:
                    self.rclone.copy_file_s3(src, dst_remote_path)
                except KeyboardInterrupt:
                    raise
                except Exception as error:
                    raise FileNotFoundError(
                        f"File not found: {src}, specified by {error}"
                    ) from error
                return

        logging.info(f"Copying {src} -> {dst}")
        src_path = src.as_posix()
        dst = dst if isinstance(dst, str) else dst.as_posix()
        result: OperationResult = self.rclone.copy_to(src_path, dst)
        if not result.ok:
            raise FileNotFoundError(f"File not found: {src}, specified by {result.error}")

    def read_bytes(self, path: Path | str) -> bytes:
        path = self._to_str(path)
        try:
            return self.rclone.read_bytes(path)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            raise FileNotFoundError(f"File not found: {path}") from error

    def write_binary(self, path: Path | str, data: bytes) -> None:
        path = self._to_str(path)
        self.rclone.write_bytes(data, path)

    def exists(self, path: Path | str) -> bool:
        path = self._to_str(path)
        return self.rclone.exists(path)

    def mkdir(self, path: str, parents=True, exist_ok=True) -> None:
        del path, parents, exist_ok

        warnings.warn("mkdir is not supported for remote backend", stacklevel=2)

    def is_dir(self, path: Path | str) -> bool:
        path = self._to_str(path)
        try:
            return self.rclone.stat(path).path.is_dir
        except FileNotFoundError:
            return False

    def is_file(self, path: Path | str) -> bool:
        path = self._to_str(path)
        try:
            return not self.rclone.stat(path).path.is_dir
        except FileNotFoundError:
            return False

    def ls(self, path: Path | str) -> tuple[list[str], list[str]]:
        path = self._to_str(path)
        try:
            listing = self.rclone.ls(path, max_depth=0)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            raise FileNotFoundError(f"File not found: {path}, because of {error}") from error
        files = [f.name for f in listing.files]
        # Directory names keep a trailing "/" marker - see FS.ls's own
        # docstring: FSPath.lspaths()/fs_walk depend on this exact
        # RemoteFS-vs-RealFS asymmetry, not despite it.
        dirs = [f"{d.name}/" for d in listing.dirs]
        return files, dirs

    def unlink(self, path: Path | str) -> None:
        self.remove(path)

    def remove(self, path: Path | str) -> None:
        """Remove a file or symbolic link."""

        path = path if isinstance(path, str) else path.as_posix()
        result = self.rclone.delete_files(path)
        if not result.ok:
            raise FileNotFoundError(f"File not found: {path}, because of {result.error}")

    def get_path(self, path: str) -> "FSPath":
        return FSPath(self, path)

    def dispose(self) -> None:
        if self.shutdown or not self.server:
            return
        self.shutdown = True
        self.server.shutdown()

    def __del__(self) -> None:
        self.dispose()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.dispose()


class FSPath:
    @staticmethod
    def from_path(path: Path | str) -> "FSPath":
        return RealFS.from_path(path)

    def __init__(self, fs: FS | Path, path: str | None = None) -> None:
        self.fs: FS
        self.path: str
        if isinstance(fs, Path):
            real_path = RealFS.from_path(fs)
            self.fs = real_path.fs
            self.path = real_path.path
        else:
            assert path is not None, "path must be non None, when not auto converting from Path"
            self.fs = fs
            self.path = path

    def read_text(self) -> str:
        data = self.read_bytes()
        return data.decode("utf-8")

    def read_bytes(self) -> bytes:
        data: bytes | None = None
        try:
            data = self.fs.read_bytes(self.path)
            return data
        except Exception as e:
            raise FileNotFoundError(f"File not found: {self.path}, because of {e}") from e

    def exists(self) -> bool:
        return self.fs.exists(self.path)

    def __str__(self) -> str:
        return self.path

    def __repr__(self) -> str:
        return f"FSPath({self.path})"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        pass

    def mkdir(self, parents=True, exist_ok=True) -> None:
        self.fs.mkdir(self.path, parents=parents, exist_ok=exist_ok)

    def walk(
        self,
    ) -> "Generator[tuple[FSPath, list[str], list[str]]]":
        return fs_walk(self)

    def walk_begin(self, max_backlog: int = 8) -> FSWalker:
        """
        Threaded walker to hide latency.

        with FSPath.walk_begin() as walker:
            for root, dirnames, filenames in walker:
                pass
        """
        return FSWalker(self, max_backlog=max_backlog)

    def _pure_path(self) -> PurePath:
        """The path-math value type for `self.path`.

        `FSPath` wraps both local filesystem paths (`RealFS`) and rclone
        remote paths (`RemoteFS`) behind one string attribute. Path-joining
        and -splitting must use the value type matching the domain: `Path`
        (native `WindowsPath`/`PosixPath`) for local paths, but always
        `PurePosixPath` for remote paths, since those are forward-slash-only
        regardless of host OS - `WindowsPath` would otherwise treat a
        literal `\\` in a remote object key as a separator on Windows.
        """
        if isinstance(self.fs, RemoteFS):
            return PurePosixPath(self.path)
        return Path(self.path)

    def relative_to(self, other: "FSPath") -> "FSPath":
        self_pure = self._pure_path()
        other_pure = type(self_pure)(other.path)
        p = self_pure.relative_to(other_pure)
        return FSPath(self.fs, p.as_posix())

    def write_text(self, data: str, encoding: str | None = None) -> None:
        if encoding is None:
            encoding = "utf-8"
        self.write_bytes(data.encode(encoding))

    def write_bytes(self, data: bytes) -> None:
        self.fs.write_binary(self.path, data)

    def rmtree(self, ignore_errors=False) -> None:
        self_exists = self.exists()
        if not ignore_errors and not self_exists:
            raise FileNotFoundError(f"Path does not exist: {self.path}")

        if isinstance(self.fs, RealFS):
            shutil.rmtree(self.path, ignore_errors=ignore_errors)
            return
        assert isinstance(self.fs, RemoteFS)

        for root, _, filenames in self.walk():
            for filename in filenames:
                path = root / filename
                path.remove()

    def lspaths(self) -> "tuple[list[FSPath], list[FSPath]]":
        filenames, dirnames = self.ls()
        fpaths: list[FSPath] = [self / name for name in filenames]
        dpaths: list[FSPath] = [self / name for name in dirnames]
        return fpaths, dpaths

    def ls(self) -> tuple[list[str], list[str]]:
        filenames: list[str]
        dirnames: list[str]
        filenames, dirnames = self.fs.ls(self.path)
        return filenames, dirnames

    def remove(self) -> None:
        """Remove a file or directory, there are subtle differences between the Real and RemoteFS."""
        self.fs.remove(self.path)

    def unlink(self) -> None:
        """Remove a file or symbolic link, there are subtle differences between the Real and RemoteFS."""
        self.fs.unlink(self.path)

    def with_suffix(self, suffix: str) -> "FSPath":
        return FSPath(self.fs, self._pure_path().with_suffix(suffix).as_posix())

    @property
    def suffix(self) -> str:
        return self._pure_path().suffix

    @property
    def name(self) -> str:
        return self._pure_path().name

    @property
    def parent(self) -> "FSPath":
        parent_path = self._pure_path().parent
        parent_str = parent_path.as_posix()
        return FSPath(self.fs, parent_str)

    def __truediv__(self, other: str) -> "FSPath":
        new_path = self._pure_path() / other
        return FSPath(self.fs, new_path.as_posix())

    def __hash__(self) -> int:
        out = hash(f"{self.fs!r}:{self.path}")
        return out

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FSPath):
            return False
        if self.fs != other.fs:
            return False
        return self.path == other.path
