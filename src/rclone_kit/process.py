import atexit
import logging
import subprocess
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Self, cast

from rclone_kit.config import Config
from rclone_kit.process_tree import terminate_process_tree
from rclone_kit.util import (
    clear_temp_config_file,
    format_command,
    get_verbose,
    make_temp_config_file,
)

logger = logging.getLogger(__name__)

_LIVE_PROCESSES: weakref.WeakSet["Process"] = weakref.WeakSet()


def _spawn_bytes_mode(cmd: list[str], kwargs: dict) -> subprocess.Popen[bytes]:
    """Launch `cmd` and assert the result is `Popen[bytes]`.

    `kwargs` is splatted rather than passed as literal keywords, so `Popen`'s
    `text`/`encoding`-based overload resolution can't see that no caller ever
    sets `text=`/`encoding=` here; every `Process` runs in bytes mode.
    """
    return cast(subprocess.Popen[bytes], subprocess.Popen(cmd, **kwargs))


@dataclass
class ProcessArgs:
    cmd: list[str]
    rclone_conf: Path | Config | None
    rclone_exe: Path
    cmd_list: list[str]
    verbose: bool | None = None
    capture_stdout: bool | None = None
    log: Path | None = None


class Process:
    def __init__(self, args: ProcessArgs) -> None:
        if not args.rclone_exe.exists():
            raise FileNotFoundError(f"rclone executable not found: {args.rclone_exe}")
        self.args = args
        self.log = args.log
        self.cleaned_up = False
        self.tempfile: Path | None = None
        rclone_conf: Path | None = None
        verbose = get_verbose(args.verbose)

        if isinstance(args.rclone_conf, Config):
            self.tempfile = make_temp_config_file()
            self.tempfile.write_text(args.rclone_conf.text, encoding="utf-8")
            rclone_conf = self.tempfile
        else:
            rclone_conf = args.rclone_conf

        self.cmd = [str(args.rclone_exe.resolve())]
        if rclone_conf:
            self.cmd += ["--config", str(rclone_conf.resolve())]
        self.cmd += args.cmd_list
        if self.args.log:
            self.args.log.parent.mkdir(parents=True, exist_ok=True)
            self.cmd += ["--log-file", str(self.args.log)]
        if verbose:
            cmd_str = format_command(self.cmd)
            logger.info("Running: %s", cmd_str)
        kwargs: dict = {"shell": False}
        if args.capture_stdout:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.STDOUT

        self.process = _spawn_bytes_mode(self.cmd, kwargs)
        _LIVE_PROCESSES.add(self)

    def __enter__(self) -> Self:
        return self

    def dispose(self) -> None:
        if self.cleaned_up:
            return
        self.cleaned_up = True
        self.terminate()
        self.wait()
        self.cleanup()
        _LIVE_PROCESSES.discard(self)

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.dispose()

    def cleanup(self) -> None:
        if tempfile := getattr(self, "tempfile", None):
            clear_temp_config_file(tempfile)

    def _kill_process_tree(self) -> None:
        """Recursively terminate the main process and all its child processes.

        Delegates to `rclone_kit.process_tree.terminate_process_tree`, the
        single, independently tested implementation of the terminate-wait-
        kill sequence shared with `rclone_kit.util.rclone_execute`.
        """
        terminate_process_tree(self.process.pid)

    def _atexit_terminate(self) -> None:
        """Kill this process's tree if it is still running.

        Called from `_cleanup_live_processes` on interpreter exit for every
        `Process` a caller never explicitly disposed.
        """
        if self.process.poll() is None:
            self._kill_process_tree()

    @property
    def pid(self) -> int:
        return self.process.pid

    def __del__(self) -> None:
        self.cleanup()

    def kill(self) -> None:
        """Forcefully terminate the process tree."""
        self._kill_process_tree()

    def terminate(self) -> None:
        """Gracefully terminate the process tree."""
        self._kill_process_tree()

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    @property
    def stdout(self) -> IO[bytes] | None:
        return self.process.stdout

    @property
    def stderr(self) -> IO[bytes] | None:
        return self.process.stderr

    def poll(self) -> int | None:
        return self.process.poll()

    def wait(self) -> int:
        return self.process.wait()

    def send_signal(self, sig: int) -> None:
        self.process.send_signal(sig)

    def __str__(self) -> str:
        state = ""
        rtn = self.process.poll()
        if rtn is None:
            state = "running"
        elif rtn != 0:
            state = f"error: {rtn}"
        else:
            state = "finished ok"
        return f"Process({self.cmd}, {state})"


def _cleanup_live_processes() -> None:
    """Terminate every `Process` still tracked in `_LIVE_PROCESSES`.

    Registered once at import time rather than per instance, so creating
    many `Process` objects over a long-lived interpreter session does not
    grow `atexit`'s internal registration list without bound.
    """
    with ThreadPoolExecutor() as executor:
        for process in list(_LIVE_PROCESSES):
            executor.submit(process._atexit_terminate)


atexit.register(_cleanup_live_processes)
