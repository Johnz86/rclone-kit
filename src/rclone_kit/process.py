import atexit
import subprocess
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from rclone_kit.config import Config
from rclone_kit.process_tree import terminate_process_tree
from rclone_kit.util import (
    clear_temp_config_file,
    format_command,
    get_verbose,
    make_temp_config_file,
)


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
        # Create a temporary config file if needed.
        if isinstance(args.rclone_conf, Config):
            self.tempfile = make_temp_config_file()
            self.tempfile.write_text(args.rclone_conf.text, encoding="utf-8")
            rclone_conf = self.tempfile
        else:
            rclone_conf = args.rclone_conf
        # assert rclone_conf.exists(), f"rclone config not found: {rclone_conf}"
        # Build the command.
        self.cmd = [str(args.rclone_exe.resolve())]
        if rclone_conf:
            self.cmd += ["--config", str(rclone_conf.resolve())]
        self.cmd += args.cmd_list
        if self.args.log:
            self.args.log.parent.mkdir(parents=True, exist_ok=True)
            self.cmd += ["--log-file", str(self.args.log)]
        if verbose:
            cmd_str = format_command(self.cmd)
            print(f"Running: {cmd_str}")
        kwargs: dict = {"shell": False}
        if args.capture_stdout:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.STDOUT

        self.process = subprocess.Popen(self.cmd, **kwargs)  # type: ignore

        # Register an atexit callback using a weak reference to avoid keeping the Process instance alive.
        self_ref = weakref.ref(self)

        def exit_cleanup():
            obj = self_ref()
            if obj is not None:
                obj._atexit_terminate()

        atexit.register(exit_cleanup)

    def __enter__(self) -> Self:
        return self

    def dispose(self) -> None:
        if self.cleaned_up:
            return
        self.cleaned_up = True
        self.terminate()
        self.wait()
        self.cleanup()

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
        """
        This method is registered via atexit and uses psutil to clean up the process tree.
        It runs in a daemon thread so that termination happens without blocking interpreter shutdown.
        """
        if self.process.poll() is None:  # Process is still running.

            def terminate_sequence():
                self._kill_process_tree()

            t = threading.Thread(target=terminate_sequence, daemon=True)
            t.start()
            t.join(timeout=3)

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
    def stdout(self) -> Any:
        return self.process.stdout

    @property
    def stderr(self) -> Any:
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
