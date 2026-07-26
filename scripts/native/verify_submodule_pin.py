"""Verify the pinned `native/rclone` submodule commit is fetchable from the
configured fork remote (`native/toolchain.toml`'s `rclone_fork_url`).

Catches a pin that points at a commit no longer reachable from the fork
(e.g. force-pushed away) before a build wastes time on it — GitHub allows
fetching an arbitrary reachable commit SHA directly, so a failed fetch here
means the pin itself is broken, not a transient network issue with a normal
branch/tag fetch.

Usage:
    uv run python scripts/native/verify_submodule_pin.py
"""

import subprocess
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SUBMODULE_DIR = _REPO_ROOT / "native" / "rclone"
_TOOLCHAIN_MANIFEST_PATH = _REPO_ROOT / "native" / "toolchain.toml"


class SubmodulePinError(Exception):
    """Raised when the pinned commit cannot be verified as fetchable."""


def _toolchain_manifest() -> dict:
    if not _TOOLCHAIN_MANIFEST_PATH.is_file():
        raise SubmodulePinError(f"Missing {_TOOLCHAIN_MANIFEST_PATH}.")
    with _TOOLCHAIN_MANIFEST_PATH.open("rb") as handle:
        return tomllib.load(handle)


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def _pinned_commit(submodule_dir: Path) -> str:
    if not (submodule_dir / ".git").exists():
        raise SubmodulePinError(
            f"{submodule_dir} is not an initialized Git submodule. "
            "Run 'git submodule update --init native/rclone'."
        )
    result = _run(["git", "rev-parse", "HEAD"], submodule_dir)
    if result.returncode != 0:
        raise SubmodulePinError(f"'git rev-parse HEAD' failed: {result.stderr.strip()}")
    return result.stdout.strip()


def verify_fork_pin(submodule_dir: Path, fork_url: str) -> str:
    """Verify `submodule_dir`'s currently checked-out commit is fetchable
    from `fork_url`. Returns the verified commit on success.
    """
    commit = _pinned_commit(submodule_dir)
    result = _run(["git", "fetch", "--depth=1", fork_url, commit], submodule_dir)
    if result.returncode != 0:
        raise SubmodulePinError(
            f"Pinned commit {commit} is not fetchable from {fork_url!r}: {result.stderr.strip()}"
        )
    return commit


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        toolchain = _toolchain_manifest()
        commit = verify_fork_pin(_SUBMODULE_DIR, toolchain["rclone_fork_url"])
    except SubmodulePinError as error:
        print(f"Submodule pin verification failed: {error}", file=sys.stderr)
        return 1
    print(f"Verified native/rclone pin {commit} is fetchable from origin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
