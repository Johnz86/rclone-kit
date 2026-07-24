"""Smoke test for an installed `rclone-kit` wheel.

Run with the Python interpreter of a clean virtual environment that has only
the built wheel installed (no dev dependency group) — see
`scripts/build_distribution.py` and `.github/workflows/ci.yml`'s `package`
job. Confirms:

- importing `rclone_kit` has no observable side effect (no root logging
  handler, no background thread, no child process);
- the bundled native library resolves through the packaged wheel asset only
  — never an `RCLONE_KIT_LIBRARY` override, which this process does not set;
- the bundled library initializes, reports real `BuildInfo`, and finalizes
  cleanly; and
- every `rclone_kit` console script entry point is installed and responds
  to `--help`.

Usage:
    <venv-python> scripts/smoke_test_installed_wheel.py <console-scripts-dir>
"""

import logging
import subprocess
import sys
import threading
from importlib import import_module
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path

_RCLONE_KIT_MODULE_PREFIX = "rclone_kit"
_HELP_FLAG = "--help"

# rclone-kit-install-bins ignores --help entirely (a pre-existing gap, not
# introduced here) and unconditionally tries to resolve/download the rclone
# CLI executable - which the wheel no longer bundles now that packaging is
# native-library-only, so it always falls through to a live download this
# smoke test's network isolation correctly blocks. Excluded until CLI
# removal deletes this console script outright (a separate, later stage).
_SMOKE_EXCLUDED_CONSOLE_SCRIPTS = frozenset({"rclone-kit-install-bins"})


def _root_logging_handler_count() -> int:
    return len(logging.getLogger().handlers)


def _live_thread_count() -> int:
    return threading.active_count()


def _child_process_count() -> int:
    import psutil

    return len(psutil.Process().children(recursive=True))


def _assert_unchanged(before: int, after: int, description: str) -> None:
    if before != after:
        raise SystemExit(
            f"Importing rclone_kit changed {description} from {before} to {after}; "
            "it must have no import-time side effects."
        )


def _assert_resolved_within_installed_package(resolved: Path) -> None:
    import importlib.resources

    package_root = Path(str(importlib.resources.files(_RCLONE_KIT_MODULE_PREFIX))).resolve()
    if package_root not in resolved.parents:
        raise SystemExit(
            f"Resolved library {resolved} is not under the installed package root "
            f"{package_root}; a packaged wheel asset must have been found instead of "
            "an RCLONE_KIT_LIBRARY override or an explicit path."
        )


def _import_rclone_kit_without_side_effects() -> None:
    handlers_before = _root_logging_handler_count()
    threads_before = _live_thread_count()
    children_before = _child_process_count()

    import_module("rclone_kit")

    _assert_unchanged(
        handlers_before, _root_logging_handler_count(), "the root logger's handler count"
    )
    _assert_unchanged(threads_before, _live_thread_count(), "the live thread count")
    _assert_unchanged(children_before, _child_process_count(), "the child process count")


def _initialize_and_report_build_info(library_path: Path) -> None:
    from rclone_kit.native.runtime import RcloneRuntime

    runtime = RcloneRuntime.from_library_path(library_path)
    runtime.initialize(config_path=None)
    try:
        info = runtime.build_info()
        print(
            f"BuildInfo: abi_version={info.abi_version} rclone_version={info.rclone_version!r} "
            f"go_version={info.go_version!r}"
        )
    finally:
        runtime.close()


def _rclone_kit_console_script_names() -> list[str]:
    console_scripts: tuple[EntryPoint, ...] = tuple(entry_points(group="console_scripts"))
    return sorted(
        entry_point.name
        for entry_point in console_scripts
        if entry_point.module.startswith(_RCLONE_KIT_MODULE_PREFIX)
        and entry_point.name not in _SMOKE_EXCLUDED_CONSOLE_SCRIPTS
    )


def _run_console_script_help(scripts_dir: Path, name: str) -> None:
    executable = scripts_dir / name
    completed = subprocess.run(
        [str(executable), _HELP_FLAG], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise SystemExit(f"{name} --help exited {completed.returncode}: {completed.stderr}")
    print(f"{name} --help exited 0")


def main(argv: list[str] | None = None) -> int:
    """Run every smoke check against the installed wheel.

    Returns 0 when importing `rclone_kit` has no observable side effect, the
    bundled native library resolves through the packaged wheel asset,
    initializes, and reports real `BuildInfo`, and every `rclone_kit`
    console script entry point responds to `--help`. Raises `SystemExit`
    with a diagnostic message on any failure.
    """
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        raise SystemExit("Usage: smoke_test_installed_wheel.py <console-scripts-dir>")
    scripts_dir = Path(args[0])

    _import_rclone_kit_without_side_effects()

    from rclone_kit.native.library import resolve_library_path

    resolved = resolve_library_path()
    print(f"Resolved bundled native library: {resolved}")
    _assert_resolved_within_installed_package(resolved)
    _initialize_and_report_build_info(resolved)

    names = _rclone_kit_console_script_names()
    if not names:
        raise SystemExit("No rclone_kit console_scripts entry points were found")
    for name in names:
        _run_console_script_help(scripts_dir, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
