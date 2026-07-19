"""Smoke test for an installed `rclone-kit` wheel.

Run with the Python interpreter of a clean virtual environment that has only
the built wheel installed (no dev dependency group) — see
`scripts/build_distribution.py` and `.github/workflows/ci.yml`'s `package`
job. Confirms the bundled rclone executable resolves and runs, and that
every `rclone_kit` console script entry point is installed and responds to
`--help`.

Usage:
    <venv-python> scripts/smoke_test_installed_wheel.py <console-scripts-dir>
"""

import subprocess
import sys
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path

import rclone_kit  # noqa: F401
from rclone_kit.runtime.rclone_binary import resolve_rclone_executable

_RCLONE_KIT_MODULE_PREFIX = "rclone_kit"
_VERSION_SUBCOMMAND = "version"
_HELP_FLAG = "--help"


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

    Returns 0 when the bundled executable resolves and runs, and every
    `rclone_kit` console script entry point responds to `--help`. Raises
    `SystemExit` with a diagnostic message on any failure.
    """
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        raise SystemExit("Usage: smoke_test_installed_wheel.py <console-scripts-dir>")
    scripts_dir = Path(args[0])

    resolved = resolve_rclone_executable()
    print(f"Resolved bundled rclone executable: {resolved}")
    _run_bundled_executable_version(resolved)

    names = _rclone_kit_console_script_names()
    if not names:
        raise SystemExit("No rclone_kit console_scripts entry points were found")
    for name in names:
        _run_console_script_help(scripts_dir, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
