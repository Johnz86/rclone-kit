import atexit
import logging
import os
import platform
import shutil
import subprocess
import time
import warnings
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from rclone_kit.process import Process
from rclone_kit.util import format_command

logger = logging.getLogger(__name__)

_SYSTEM = platform.system()
_WINDOWS = "Windows"
_LINUX = "Linux"
_DARWIN = "Darwin"

_FUSERMOUNT_COMMANDS = ("fusermount3", "fusermount")
_LINUX_FUSE_DEVICE_PATH = Path("/dev/fuse")
_WINFSP_RELATIVE_DLL_PATHS = (
    Path("WinFsp") / "bin" / "winfsp-x64.dll",
    Path("WinFsp") / "bin" / "winfsp-x86.dll",
)
_PROGRAM_FILES_ENV_VARS = ("ProgramFiles", "ProgramFiles(x86)")

_MOUNTS_FOR_GC: weakref.WeakSet = weakref.WeakSet()


class MountStatus(Protocol):
    process: Process
    mount_path: Path


class MountResource(MountStatus, Protocol):
    def close(self, wait: bool = True) -> None: ...


class MountPrerequisiteError(RuntimeError):
    """Raised when the operating-system mount facility rclone's `mount`
    subcommand depends on is not available, so attempting to mount would
    otherwise fail with an opaque subprocess error instead of a clear one.
    """

    def __init__(self, operating_system: str, requirement: str) -> None:
        self.operating_system = operating_system
        self.requirement = requirement
        super().__init__(
            f"Mounting on {operating_system} requires {requirement}, which was not detected."
        )


def is_winfsp_available(program_files_dirs: tuple[Path, ...] | None = None) -> bool:
    """Return whether the WinFsp mount launcher appears installed.

    rclone's Windows mount backend requires WinFsp; without it, `rclone
    mount` fails with an opaque native error rather than a clear message.
    Always `False` off Windows. Checks for WinFsp's DLL under each of
    `program_files_dirs`, which defaults to the real `%ProgramFiles%` and
    `%ProgramFiles(x86)%` directories; overriding it allows this check to be
    exercised in tests without touching the real filesystem.
    """
    if _SYSTEM != _WINDOWS:
        return False
    roots = program_files_dirs if program_files_dirs is not None else _real_program_files_dirs()
    return any(
        (root / relative_dll_path).is_file()
        for root in roots
        for relative_dll_path in _WINFSP_RELATIVE_DLL_PATHS
    )


def _real_program_files_dirs() -> tuple[Path, ...]:
    return tuple(Path(value) for var in _PROGRAM_FILES_ENV_VARS if (value := os.environ.get(var)))


def is_fuse_available(fuse_device_path: Path | None = None) -> bool:
    """Return whether FUSE and a usable unmount command are available.

    Checks for the `/dev/fuse` character device and at least one of
    `fusermount3` or `fusermount` on `PATH`. Always `False` off Linux.
    `fuse_device_path` overrides the device path for testing.
    """
    if _SYSTEM != _LINUX:
        return False
    device_path = fuse_device_path if fuse_device_path is not None else _LINUX_FUSE_DEVICE_PATH
    has_unmount_command = any(shutil.which(command) is not None for command in _FUSERMOUNT_COMMANDS)
    return device_path.exists() and has_unmount_command


def ensure_mount_supported() -> None:
    """Raise `MountPrerequisiteError` when the current platform lacks the
    operating-system mount facility rclone's `mount` subcommand requires.

    A no-op on platforms this module does not certify a specific mount
    prerequisite for (e.g. macOS), leaving diagnosis to rclone's own
    subprocess failure in that case.
    """
    if _SYSTEM == _WINDOWS and not is_winfsp_available():
        raise MountPrerequisiteError(_WINDOWS, "WinFsp (https://winfsp.dev)")
    if _SYSTEM == _LINUX and not is_fuse_available():
        raise MountPrerequisiteError(
            _LINUX, "FUSE and a usable unmount command (fusermount3 or fusermount)"
        )


def _cleanup_mounts() -> None:
    with ThreadPoolExecutor() as executor:
        mount: MountResource
        for mount in _MOUNTS_FOR_GC:
            executor.submit(mount.close)


def _register_exit_cleanup_handlers() -> None:
    """Register this module's `atexit` handler, once, at import time.

    Wrapped in a named function rather than left as a bare
    `atexit.register(...)` statement, so this module's exit-time side
    effect is discoverable by name instead of blending into the
    surrounding statement flow. Placed immediately after `_cleanup_mounts`
    rather than after the unrelated helpers that follow it, so the
    definition and its registration read together.
    """
    atexit.register(_cleanup_mounts)


_register_exit_cleanup_handlers()


def _run_command(cmd: list[str], verbose: bool) -> int:
    """Run `cmd` as an argument list (never through a shell) and print its
    output if `verbose` is `True`.

    Returns the process return code, or -1 when the executable could not be
    started at all (for example, it is not installed).
    """
    if verbose:
        logger.info("Executing: %s", subprocess.list2cmdline(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    except OSError as error:
        warnings.warn(f"Error running command {cmd!r}: {error}", stacklevel=2)
        return -1
    if result.returncode != 0 and verbose:
        logger.info("Command failed: %s\nStdErr: %s", cmd, result.stderr.strip())
    return result.returncode


def cache_dir_delete_on_exit(cache_dir: Path) -> None:
    if cache_dir.exists():
        try:
            shutil.rmtree(cache_dir, ignore_errors=True)
        except OSError as e:
            warnings.warn(f"Error removing cache directory {cache_dir}: {e}", stacklevel=2)


def add_mount_for_gc(mount: MountResource) -> None:

    _MOUNTS_FOR_GC.add(mount)


def remove_mount_for_gc(mount: MountResource) -> None:
    _MOUNTS_FOR_GC.discard(mount)


def prepare_mount(outdir: Path, verbose: bool) -> None:
    if _SYSTEM == "Windows":
        if verbose:
            logger.info("Creating parent directories for %s", outdir)
        outdir.parent.mkdir(parents=True, exist_ok=True)
    else:
        if verbose:
            logger.info("Creating directories for %s", outdir)
        outdir.mkdir(parents=True, exist_ok=True)


def wait_for_mount(
    mount: MountStatus,
    timeout: int = 20,
    post_mount_delay: int = 5,
    poll_interval: float = 1.0,
    check_mount_flag: bool = False,
) -> None:
    """
    Wait for a mount point to become available by checking if the directory exists,
    optionally verifying that it is a mount point, and confirming that it contains files.
    This function periodically polls for the mount status, ensures the mount process
    is still running, and applies an extra delay after detecting content for stabilization.

    Args:
        src (Path): The mount point directory to check.
        mount_process (Any): A Process instance handling the mount (must be an instance of Process).
        timeout (int): Maximum time in seconds to wait for the mount to become available.
        post_mount_delay (int): Additional seconds to wait after detecting files.
        poll_interval (float): Seconds between each poll iteration.
        check_mount_flag (bool): If True, verifies that the path is recognized as a mount point.

    Raises:
        subprocess.CalledProcessError: If the mount_process exits unexpectedly.
        TimeoutError: If the mount is not available within the timeout period.
        TypeError: If mount_process is not an instance of Process.
    """

    mount_process = mount.process
    src = mount.mount_path

    if not isinstance(mount_process, Process):
        raise TypeError("mount_process must be an instance of Process")

    expire_time = time.monotonic() + timeout
    last_error = None

    while time.monotonic() < expire_time:
        rtn = mount_process.poll()
        if rtn is not None:
            logger.error(
                "Mount process terminated unexpectedly: %s", format_command(mount_process.cmd)
            )
            raise subprocess.CalledProcessError(rtn, mount_process.cmd)

        if src.exists():
            if check_mount_flag:
                try:
                    if not os.path.ismount(str(src)):
                        logger.debug("%s exists but is not recognized as a mount point yet.", src)
                        time.sleep(poll_interval)
                        continue
                except OSError as e:
                    logger.warning("Could not verify mount point status for %s: %s", src, e)

            try:
                if any(src.iterdir()):
                    logger.info(
                        "Mount point %s appears available with files. Waiting %d seconds for stabilization.",
                        src,
                        post_mount_delay,
                    )
                    time.sleep(post_mount_delay)
                    return
                else:
                    logger.debug("Mount point %s is empty. Waiting for files to appear.", src)
            except OSError as e:
                last_error = e
                logger.warning("Error accessing %s: %s", src, e)
        else:
            logger.debug("Mount point %s does not exist yet.", src)

        time.sleep(poll_interval)

    message = f"Mount point {src} did not become available within {timeout} seconds"
    if last_error is not None:
        raise TimeoutError(message) from last_error
    raise TimeoutError(message)


def _rmtree_ignore_mounts(path):
    """
    Recursively remove a directory tree while ignoring mount points.

    Directories that are mount points (where os.path.ismount returns True)
    are skipped.
    """

    with os.scandir(path) as it:
        for entry in it:
            full_path = entry.path
            if entry.is_dir(follow_symlinks=False):
                if os.path.ismount(full_path):
                    logger.debug("Skipping mount point: %s", full_path)
                    continue

                _rmtree_ignore_mounts(full_path)
            else:
                os.unlink(full_path)

    os.rmdir(path)


def clean_mount(mount: MountStatus | Path, verbose: bool = False, wait=True) -> None:
    """
    Clean up a mount path across Linux, macOS, and Windows.

    The function attempts to unmount the mount at mount_path, then, if the
    directory is empty, removes it. On Linux it uses 'fusermount -u' (for FUSE mounts)
    and 'umount'. On macOS it uses 'umount' (and optionally 'diskutil unmount'),
    while on Windows it attempts to remove the mount point via 'mountvol /D'.
    """

    def verbose_print(msg: str):
        if verbose:
            logger.info(msg)

    proc = None if isinstance(mount, Path) else mount.process

    if proc is not None and proc.poll() is None:
        verbose_print(f"Terminating mount process {proc.pid}")
        proc.kill()

    mount_path = mount if isinstance(mount, Path) else mount.mount_path
    try:
        mount_exists = mount_path.exists()
    except OSError:
        mount_exists = True

    if wait:
        time.sleep(2)

    if not mount_exists:
        verbose_print(f"{mount_path} does not exist; nothing to clean up.")
        return

    verbose_print(f"{mount_path} still exists, attempting to unmount and remove.")

    if _SYSTEM == _LINUX:
        _run_command(["fusermount", "-u", str(mount_path)], verbose)
        _run_command(["umount", str(mount_path)], verbose)
    elif _SYSTEM == _DARWIN:
        _run_command(["umount", str(mount_path)], verbose)

    elif _SYSTEM == _WINDOWS:
        _run_command(["mountvol", str(mount_path), "/D"], verbose)

        try:
            _rmtree_ignore_mounts(mount_path)
            if mount_path.exists():
                raise OSError(f"Failed to remove mount directory {mount_path}")
            if verbose:
                logger.info("Successfully removed mount directory %s", mount_path)
        except OSError as error:
            warnings.warn(f"Failed to remove mount {mount_path}: {error}", stacklevel=2)
    else:
        warnings.warn(f"Unsupported platform: {_SYSTEM}", stacklevel=2)

    if wait:
        time.sleep(2)

    try:
        still_exists = mount_path.exists()
    except OSError as e:
        warnings.warn(f"Error re-checking {mount_path}: {e}", stacklevel=2)
        still_exists = True

    if still_exists:
        verbose_print(f"{mount_path} still exists after unmount attempt.")

        if not any(mount_path.iterdir()):
            try:
                mount_path.rmdir()
            except OSError as e:
                warnings.warn(f"Error removing mount {mount_path}: {e}", stacklevel=2)
                raise
            if verbose:
                verbose_print(f"Removed empty mount directory {mount_path}")
        else:
            warnings.warn(f"{mount_path} is not empty; cannot remove.", stacklevel=2)
            raise OSError(f"{mount_path} is not empty")

    else:
        verbose_print(f"{mount_path} successfully cleaned up.")
