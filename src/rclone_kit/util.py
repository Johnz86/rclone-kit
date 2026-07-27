from __future__ import annotations

import atexit
import contextlib
import os
import secrets
import shutil
import signal
import string
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from rclone_kit.dir import Dir
from rclone_kit.rc.paths import split_remote_and_path
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath

if TYPE_CHECKING:
    from rclone_kit.access import ListingAccess

_TMP_CONFIG_DIR_PREFIX = "rclone-kit-config-"
_RCLONE_CONFIGS_LIST: list[Path] = []
_DO_CLEANUP = os.getenv("RCLONE_KIT_CLEANUP", "1") == "1"
_DEFAULT_CONFIG_FILENAME = "rclone.conf"


def make_atexit_registrar(*handlers: Callable[[], None], doc: str) -> Callable[[], None]:
    """Build a thread-safe "register these `atexit` handlers exactly once" callable.

    Each returned callable owns its own lock and a function-attribute
    registration flag (the same lock-plus-flag idiom
    `chunk_store.get_chunk_tmpdir` uses for its own first-use guard, stored
    on the callable's own `__dict__` so tests can reset it), so independent
    call sites - config cleanup, process cleanup, chunk-file cleanup - never
    contend with each other, and concurrent callers of the same site only
    ever trigger one `atexit.register` per handler. `doc` becomes the
    returned callable's docstring, since a bare module-level assignment has
    nowhere else to carry the call site's own rationale.
    """
    lock = Lock()

    def _register() -> None:
        with lock:
            state = _register.__dict__
            if state.get("registered"):
                return
            for handler in handlers:
                atexit.register(handler)
            state["registered"] = True

    _register.__doc__ = doc
    return _register


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


_register_exit_cleanup_handlers = make_atexit_registrar(
    _clean_configs,
    doc="""Register this module's `atexit` handlers, once, the first time
    a temp config directory is created.

    Called from `make_temp_config_file`, the sole producer of
    `_RCLONE_CONFIGS_LIST`, rather than at import time, so a process that
    merely imports `rclone_kit` without ever creating a temp config file
    never wires up the handler.
    """,
)


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
    _register_exit_cleanup_handlers()
    tmpdir = Path(tempfile.mkdtemp(prefix=_TMP_CONFIG_DIR_PREFIX))
    _RCLONE_CONFIGS_LIST.append(tmpdir)
    config_path = tmpdir / "rclone.conf"
    config_path.touch(mode=0o600, exist_ok=False)
    return config_path


def to_path(item: Dir | Remote | str, rclone: ListingAccess) -> RPath:
    if isinstance(item, str):
        remote_name, path = split_remote_and_path(item)
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


def default_config_path(config: Path | None) -> Path:
    """Default an rclone config CLI argument to `rclone.conf`."""
    return config if config is not None else Path(_DEFAULT_CONFIG_FILENAME)


def validate_config_path_exists(config: Path) -> None:
    """Raise `FileNotFoundError` if `config` does not exist."""
    if not config.exists():
        raise FileNotFoundError(f"Config file not found: {config}")


def random_str(length: int) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def write_files_from(tmpdir: Path | str, files: list[str]) -> Path:
    """Write `files`, one per line, to `include_files.txt` inside `tmpdir`.

    Returns the path to pass to rclone's `--files-from` flag.
    """
    include_files_txt = Path(tmpdir) / "include_files.txt"
    include_files_txt.write_text("\n".join(files), encoding="utf-8")
    return include_files_txt
