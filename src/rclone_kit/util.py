import atexit
import contextlib
import logging
import os
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import warnings
import weakref
from pathlib import Path
from threading import Lock
from typing import Any

from rclone_kit.config import Config
from rclone_kit.dir import Dir
from rclone_kit.process_tree import terminate_process_tree
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath
from rclone_kit.runtime.rclone_binary import resolve_rclone_executable
from rclone_kit.types import S3PathInfo

logger = logging.getLogger(__name__)

_PRINT_LOCK = Lock()

_TMP_CONFIG_DIR_PREFIX = "rclone-kit-config-"
_RCLONE_CONFIGS_LIST: list[Path] = []
_DO_CLEANUP = os.getenv("RCLONE_KIT_CLEANUP", "1") == "1"
_REDACTED_VALUE = "<redacted>"
_SENSITIVE_FLAG_PARTS = frozenset({"auth", "password", "pass", "secret", "token"})
_SENSITIVE_COMPOUND_FLAGS = ("access-key", "private-key")


def _clean_configs(signum: int | None = None, _frame: object | None = None) -> None:
    """Remove every temporary config directory created by this process.

    Safe to call more than once; `RCLONE_KIT_CLEANUP=0` disables it entirely.
    When invoked as a signal handler (`signum` given), restores the default
    disposition for `signum` and re-raises it against this process after
    cleaning up, so the process still terminates the way it would have
    without this handler installed.
    """
    if not _DO_CLEANUP:
        return
    while _RCLONE_CONFIGS_LIST:
        config_dir = _RCLONE_CONFIGS_LIST.pop()
        with contextlib.suppress(OSError):
            shutil.rmtree(config_dir, ignore_errors=True)
    if signum is not None:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


atexit.register(_clean_configs)


def register_signal_cleanup() -> None:
    """Register `SIGINT`/`SIGTERM` handlers that clean up temporary rclone
    config directories before re-raising the signal.

    Must be called explicitly from an application entry point (the packaged
    console scripts under `rclone_kit.cmd` call it from `main()`); it is
    never registered as a package-import side effect.

    Raises `RuntimeError` when called from any thread other than the main
    thread, since `signal.signal` only accepts handler registration there.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("register_signal_cleanup must be called from the main thread")
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _clean_configs)


def make_temp_config_file() -> Path:
    """Create a fresh `rclone.conf` file inside a new process-private
    temporary directory and register that directory for exit/signal cleanup.

    Uses `tempfile.mkdtemp`, so the directory is created under the operating
    system's temporary directory rather than the current working directory.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix=_TMP_CONFIG_DIR_PREFIX))
    _RCLONE_CONFIGS_LIST.append(tmpdir)
    config_path = tmpdir / "rclone.conf"
    config_path.touch(mode=0o600, exist_ok=False)
    return config_path


def clear_temp_config_file(path: Path | None) -> None:
    """Delete a temporary config file created by `make_temp_config_file`.

    A no-op when `path` is `None` or cleanup is disabled via
    `RCLONE_KIT_CLEANUP=0`. Idempotent: safe to call more than once for the
    same path.
    """
    if path is None or not _DO_CLEANUP:
        return
    config_dir = path.parent
    if config_dir not in _RCLONE_CONFIGS_LIST:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        return
    with contextlib.suppress(OSError):
        shutil.rmtree(config_dir)
    with contextlib.suppress(ValueError):
        _RCLONE_CONFIGS_LIST.remove(config_dir)


def _is_sensitive_flag(flag: str) -> bool:
    normalized = flag.lstrip("-").lower()
    parts = frozenset(normalized.split("-"))
    return bool(parts & _SENSITIVE_FLAG_PARTS) or any(
        compound in normalized for compound in _SENSITIVE_COMPOUND_FLAGS
    )


def format_command(command: list[str]) -> str:
    """Format an argument vector for diagnostics with credential values redacted."""
    redacted: list[str] = []
    redact_next = False
    for argument in command:
        if redact_next:
            redacted.append(_REDACTED_VALUE)
            redact_next = False
            continue
        flag, separator, _value = argument.partition("=")
        if argument.startswith("-") and _is_sensitive_flag(flag):
            if separator:
                redacted.append(f"{flag}={_REDACTED_VALUE}")
            else:
                redacted.append(argument)
                redact_next = True
            continue
        redacted.append(argument)
    return subprocess.list2cmdline(redacted)


def locked_print(*args: Any, **kwargs: Any) -> None:
    with _PRINT_LOCK:
        print(*args, **kwargs)


def port_is_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) != 0


_FREE_PORT_RANGE_START = 10000
_FREE_PORT_RANGE_END = 20000
_FREE_PORT_MAX_ATTEMPTS = 20


def _random_port() -> int:
    span = _FREE_PORT_RANGE_END - _FREE_PORT_RANGE_START + 1
    return _FREE_PORT_RANGE_START + secrets.randbelow(span)


def find_free_port() -> int:
    port = _random_port()
    for _attempt in range(_FREE_PORT_MAX_ATTEMPTS):
        if port_is_free(port):
            return port
        port = _random_port()
    warnings.warn(f"Failed to find a free port, so using {port}", stacklevel=2)
    return port


def to_path(item: Dir | Remote | str, rclone: Any) -> RPath:
    from rclone_kit.rclone_impl import RcloneImpl

    assert isinstance(rclone, RcloneImpl)

    if isinstance(item, str):
        parts = item.split(":")
        remote_name = parts[0]
        path = ":".join(parts[1:])
        remote = Remote(name=remote_name, rclone=rclone)
        out = RPath(
            remote=remote,
            path=path,
            name="",
            size=0,
            mime_type="",
            mod_time="",
            is_dir=True,
        )
        out.set_rclone(rclone)
        return out
    elif isinstance(item, Dir):
        return item.path
    elif isinstance(item, Remote):
        out = RPath(
            remote=item,
            path=str(item),
            name=str(item),
            size=0,
            mime_type="inode/directory",
            mod_time="",
            is_dir=True,
        )
        out.set_rclone(rclone)
        return out
    else:
        raise ValueError(f"Invalid type for item: {type(item)}")


def get_verbose(verbose: bool | None) -> bool:
    if verbose is not None:
        return verbose

    return bool(int(os.getenv("RCLONE_KIT_VERBOSE", "0")))


def get_check(check: bool | None) -> bool:
    if check is not None:
        return check

    return bool(int(os.getenv("RCLONE_KIT_CHECK", "1")))


def get_rclone_exe(
    rclone_exe: Path | None,
    *,
    allow_path_lookup: bool = True,
    allow_verified_download: bool = False,
) -> Path:
    """Resolve the rclone executable to use for a session.

    Delegates to `rclone_kit.runtime.rclone_binary.resolve_rclone_executable`.
    `rclone_exe`, when given, is used as an explicit, authoritative override.
    Otherwise resolution tries, in order:

    1. The executable bundled with the installed wheel, verified against its
       packaged SHA-256 manifest. This is the deterministic, offline-capable
       default for an installed wheel.
    2. A `PATH` lookup, enabled by default here (`allow_path_lookup=True`) so
       a source checkout with no bundled wheel asset keeps resolving an
       already-installed system rclone, matching this function's historical
       behavior. Pass `allow_path_lookup=False` to require the bundled
       executable.
    3. A checksum-verified download into the runtime cache, only when
       `allow_verified_download=True`. This replaces the previous behavior
       of silently fetching the unpinned, unverified `rclone-current-*`
       build; downloading is now opt-in and always verified.

    Raises `RcloneResolutionError` when every enabled strategy fails.
    """
    return resolve_rclone_executable(
        explicit_path=rclone_exe,
        allow_path_lookup=allow_path_lookup,
        allow_verified_download=allow_verified_download,
    )


def upgrade_rclone() -> Path:
    """Install the certified rclone build for this platform into the runtime
    cache via a checksum-verified download, and return its path.

    Unlike the legacy implementation, this never fetches the mutable
    `rclone-current-*` build: it downloads and verifies the pinned
    `rclone_kit.runtime.platform.RCLONE_VERSION` release. When a bundled
    wheel executable already satisfies that version, the verified bundled
    copy is returned instead of performing a redundant download.
    """
    return resolve_rclone_executable(allow_verified_download=True)


def rclone_execute(
    cmd: list[str],
    rclone_conf: Path | Config | None,
    rclone_exe: Path,
    check: bool,
    capture: bool | Path | None = None,
    verbose: bool | None = None,
) -> subprocess.CompletedProcess:
    tmpfile: Path | None = None
    verbose = get_verbose(verbose)

    output_file: Path | None = None
    if isinstance(capture, Path):
        output_file = capture
        capture = False
    else:
        capture = capture if isinstance(capture, bool) else True

    file_handle = None
    try:
        if isinstance(rclone_conf, Config):
            tmpfile = make_temp_config_file()
            tmpfile.write_text(rclone_conf.text, encoding="utf-8")
            rclone_conf = tmpfile

        full_cmd = [str(rclone_exe.resolve())]
        if rclone_conf:
            full_cmd += ["--config", str(rclone_conf.resolve())]
        full_cmd += cmd
        if verbose:
            cmd_str = format_command(full_cmd)
            logger.info("Running: %s", cmd_str)

        proc_kwargs: dict[str, Any] = {
            "encoding": "utf-8",
            "shell": False,
            "stderr": subprocess.PIPE,
        }
        if output_file:
            file_handle = output_file.open("w", encoding="utf-8")
            proc_kwargs["stdout"] = file_handle
        else:
            proc_kwargs["stdout"] = subprocess.PIPE if capture else None

        process = subprocess.Popen(full_cmd, **proc_kwargs)

        proc_ref = weakref.ref(process)

        def cleanup() -> None:
            proc = proc_ref()
            if proc is None:
                return
            terminate_process_tree(proc.pid)

        atexit.register(cleanup)

        out, err = process.communicate()

        cp: subprocess.CompletedProcess = subprocess.CompletedProcess(
            args=full_cmd,
            returncode=process.returncode,
            stdout=out,
            stderr=err,
        )

        if cp.returncode != 0:
            cmd_str = format_command(full_cmd)
            warnings.warn(
                f"Error running: {cmd_str}, returncode: {cp.returncode}\n{cp.stdout}\n{cp.stderr}",
                stacklevel=2,
            )
            if check:
                raise subprocess.CalledProcessError(cp.returncode, full_cmd, cp.stdout, cp.stderr)
        return cp
    finally:
        if file_handle is not None:
            file_handle.close()
        clear_temp_config_file(tmpfile)


def split_s3_path(path: str) -> S3PathInfo:
    if ":" not in path:
        raise ValueError(f"Invalid S3 path: {path}")

    prts = path.split(":", 1)
    remote = prts[0]
    path = prts[1]
    parts: list[str] = []
    for raw_part in path.split("/"):
        part = raw_part.strip()
        if part:
            parts.append(part)
    if len(parts) < 2:
        raise ValueError(f"Invalid S3 path: {path}")
    bucket = parts[0]
    key = "/".join(parts[1:])
    assert bucket
    assert key
    return S3PathInfo(remote=remote, bucket=bucket, key=key)


def random_str(length: int) -> str:
    import string

    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
