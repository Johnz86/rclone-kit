"""Regression tests proving the four import-reachable `atexit` cleanup
handlers (`rclone_kit.util._clean_configs`/`_terminate_live_subprocesses`,
`rclone_kit.process._cleanup_live_processes`,
`rclone_kit.file_part._on_exit_cleanup`) register lazily, on first use of the
resource they protect, rather than unconditionally at `import rclone_kit`
time.

`atexit` registration state and module import state are both process-global
and cannot be reset within a single interpreter, so proving "not registered
merely by import" requires a fresh subprocess - the same reason
`scripts/smoke_test_installed_wheel.py` inspects import-time thread/process
counts from a fresh interpreter rather than the current one. The probe
script below spies on `atexit.register` before importing `rclone_kit`,
confirms none of the four target handlers are registered by the bare
import, then exercises each handler's sole registry producer in turn
(`make_temp_config_file`, constructing a `Process`, constructing a
`FilePart`) and confirms each one becomes registered only once its producer
actually runs - and not before.

`mount_util._cleanup_mounts` and `upload_parts_resumable._cleanup_tmp_upload_dirs`
are deliberately out of scope: both modules are already only ever imported
lazily at their one real call site, so their registration already
coincides with first use without needing this change.
"""

import subprocess
import sys
from pathlib import Path

from rclone_kit.file_part import _on_exit_cleanup
from rclone_kit.process import _cleanup_live_processes
from rclone_kit.util import _clean_configs, _terminate_live_subprocesses

_SUBPROCESS_TIMEOUT_SECONDS = 30
_SUCCESS_MARKER = "ATEXIT_LAZY_REGISTRATION_PROBE_OK"


def _handler_id(handler: object) -> str:
    return f"{handler.__module__}.{handler.__qualname__}"


_CLEAN_CONFIGS_ID = _handler_id(_clean_configs)
_TERMINATE_LIVE_SUBPROCESSES_ID = _handler_id(_terminate_live_subprocesses)
_CLEANUP_LIVE_PROCESSES_ID = _handler_id(_cleanup_live_processes)
_ON_EXIT_CLEANUP_ID = _handler_id(_on_exit_cleanup)

_TARGET_HANDLER_IDS = (
    _CLEAN_CONFIGS_ID,
    _TERMINATE_LIVE_SUBPROCESSES_ID,
    _CLEANUP_LIVE_PROCESSES_ID,
    _ON_EXIT_CLEANUP_ID,
)

_PROBE_SCRIPT = """
import atexit
import sys
from pathlib import Path

registered: list[str] = []
_original_register = atexit.register


def _spy_register(fn, *args, **kwargs):
    registered.append(f"{fn.__module__}.{fn.__qualname__}")
    return _original_register(fn, *args, **kwargs)


atexit.register = _spy_register

import rclone_kit  # noqa: F401
from rclone_kit import util
from rclone_kit.file_part import FilePart
from rclone_kit.process import Process, ProcessArgs
from rclone_kit.s3.multipart.file_info import S3FileInfo

target_ids = __TARGET_IDS__
clean_configs_id, terminate_live_subprocesses_id, cleanup_live_processes_id, on_exit_cleanup_id = (
    target_ids
)

already_registered = set(target_ids) & set(registered)
assert not already_registered, f"registered merely by import: {sorted(already_registered)}"

util.make_temp_config_file()
assert clean_configs_id in registered, "make_temp_config_file did not register _clean_configs"
assert terminate_live_subprocesses_id in registered, (
    "make_temp_config_file did not register _terminate_live_subprocesses"
)
assert cleanup_live_processes_id not in registered, (
    "creating a temp config file must not register the Process handler"
)
assert on_exit_cleanup_id not in registered, (
    "creating a temp config file must not register the FilePart handler"
)

proc = Process(
    ProcessArgs(
        cmd=[],
        rclone_conf=None,
        rclone_exe=Path(sys.executable),
        cmd_list=["--version"],
        capture_stdout=True,
    )
)
proc.wait()
proc.dispose()
assert cleanup_live_processes_id in registered, "Process construction did not register its handler"
assert on_exit_cleanup_id not in registered, (
    "constructing a Process must not register the FilePart handler"
)

FilePart(payload=b"probe-bytes", extra=S3FileInfo(upload_id="probe", part_number=1))
assert on_exit_cleanup_id in registered, "FilePart construction did not register its handler"

print("__SUCCESS_MARKER__")
"""


def test_atexit_handlers_register_lazily_not_at_import(tmp_path: Path) -> None:
    script = _PROBE_SCRIPT.replace("__TARGET_IDS__", repr(list(_TARGET_HANDLER_IDS))).replace(
        "__SUCCESS_MARKER__", _SUCCESS_MARKER
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 0, (
        f"probe subprocess failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert _SUCCESS_MARKER in result.stdout
