"""Cross-platform process-tree termination.

Both `rclone_kit.process.Process` and `rclone_kit.util.rclone_execute` need
to terminate a subprocess and every descendant it spawned when cleaning up a
still-running rclone invocation. Sharing one implementation here keeps the
terminate-wait-kill sequence single-sourced, testable against a fake
`psutil.Process`, and free of the import cycle that would result from
`util.py` importing from `process.py` (which already imports from
`util.py`).
"""

import contextlib

import psutil

_CHILD_TERMINATE_WAIT_SECONDS = 2.0
_PARENT_TERMINATE_WAIT_SECONDS = 3.0


def terminate_process_tree(
    pid: int,
    *,
    child_wait_seconds: float = _CHILD_TERMINATE_WAIT_SECONDS,
    parent_wait_seconds: float = _PARENT_TERMINATE_WAIT_SECONDS,
) -> None:
    """Terminate the process tree rooted at `pid`.

    Sends a terminate signal to every child process (recursively) first,
    waits up to `child_wait_seconds` for them to exit, and force-kills any
    survivor. The same terminate-wait-kill sequence is then applied to `pid`
    itself, waiting up to `parent_wait_seconds`.

    A no-op when `pid` does not refer to a running process. Per-process
    `psutil.Error` failures (a process exiting concurrently, for example) are
    swallowed so best-effort cleanup does not itself raise; unrelated
    exceptions propagate.
    """
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return

    _terminate_and_escalate(children, child_wait_seconds)

    with contextlib.suppress(psutil.NoSuchProcess):
        if parent.is_running():
            _terminate_and_escalate([parent], parent_wait_seconds)


def _terminate_and_escalate(processes: list[psutil.Process], wait_seconds: float) -> None:
    if not processes:
        return
    for process in processes:
        with contextlib.suppress(psutil.Error):
            process.terminate()
    _gone, alive = psutil.wait_procs(processes, timeout=wait_seconds)
    for process in alive:
        with contextlib.suppress(psutil.Error):
            process.kill()
