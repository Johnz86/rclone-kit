"""Console script that installs the certified rclone executable.

Superseded the legacy `rclone_download` implementation, which fetched an
unpinned, unverified `rclone-current-*` build into a caller-chosen path. This
script now resolves the pinned, checksum-verified build through
`rclone_kit.runtime.rclone_binary.resolve_rclone_executable`, allowing a
verified download so the command works from a source checkout that has no
bundled wheel asset.
"""

import logging

from rclone_kit.runtime.rclone_binary import resolve_rclone_executable
from rclone_kit.util import register_signal_cleanup

logger = logging.getLogger(__name__)


def main() -> int:
    """Install the certified rclone executable for the current platform.

    Resolves the bundled wheel executable when available, otherwise performs
    a checksum-verified download into the runtime cache. Prints the resolved
    executable path.

    Returns:
        0 on success.
    """
    register_signal_cleanup()
    logging.basicConfig(level=logging.DEBUG)
    resolved_path = resolve_rclone_executable(allow_verified_download=True)
    print(f"rclone executable ready at: {resolved_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
