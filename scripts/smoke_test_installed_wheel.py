"""Smoke test for an installed `rclone-kit` wheel.

Run with the Python interpreter of a clean virtual environment that has only
the built wheel installed (no dev dependency group) — see
`scripts/build_distribution.py` and `.github/workflows/ci.yml`'s `package`
job. Confirms:

- importing `rclone_kit` has no observable side effect (no root logging
  handler, no background thread, no child process);
- the bundled rclone executable resolves through the packaged-asset cache
  only — never a `PATH` lookup or a runtime download, both of which
  `rclone_kit.runtime.rclone_binary.resolve_rclone_executable` already
  disables by default, verified here by asserting the resolved path;
- the bundled executable runs; and
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
_VERSION_SUBCOMMAND = "version"
_HELP_FLAG = "--help"


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


def _assert_resolved_via_bundled_asset_cache(resolved: Path) -> None:
    from rclone_kit.runtime.rclone_binary import default_cache_root

    cache_root = default_cache_root()
    if cache_root not in resolved.parents:
        raise SystemExit(
            f"Resolved executable {resolved} is not under the bundled-asset cache "
            f"root {cache_root}; a PATH lookup or runtime download must not have "
            "occurred (both are disabled by default)."
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


def _run_bundled_executable_version(executable: Path) -> None:
    result = subprocess.run(
        [str(executable), _VERSION_SUBCOMMAND], capture_output=True, text=True, check=True
    )
    print(result.stdout.splitlines()[0])


def _rclone_kit_console_script_names() -> list[str]:
    console_scripts: tuple[EntryPoint, ...] = tuple(entry_points(group="console_scripts"))
    return sorted(
        entry_point.name
        for entry_point in console_scripts
        if entry_point.module.startswith(_RCLONE_KIT_MODULE_PREFIX)
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
    bundled executable resolves through the packaged-asset cache and runs,
    and every `rclone_kit` console script entry point responds to `--help`.
    Raises `SystemExit` with a diagnostic message on any failure.
    """
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        raise SystemExit("Usage: smoke_test_installed_wheel.py <console-scripts-dir>")
    scripts_dir = Path(args[0])

    _import_rclone_kit_without_side_effects()

    from rclone_kit.runtime.rclone_binary import resolve_rclone_executable

    resolved = resolve_rclone_executable()
    print(f"Resolved bundled rclone executable: {resolved}")
    _assert_resolved_via_bundled_asset_cache(resolved)
    _run_bundled_executable_version(resolved)

    names = _rclone_kit_console_script_names()
    if not names:
        raise SystemExit("No rclone_kit console_scripts entry points were found")
    for name in names:
        _run_console_script_help(scripts_dir, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
