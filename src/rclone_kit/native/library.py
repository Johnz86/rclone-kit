"""Native library path resolution.

Resolution order, per `reference/rclone_c_abi_implementation_plan.md`'s
"Library loading" section:

1. an explicit `library_path` passed by the caller;
2. the `RCLONE_KIT_LIBRARY` development override;
3. a packaged wheel asset (not implemented yet — no wheel staging exists
   until Phase 3's wheel-packaging work lands).

Always returns an absolute path to an existing file; never searches `PATH`
or the current working directory.
"""

import os
from pathlib import Path

from rclone_kit.native.errors import LibraryNotFoundError

RCLONE_KIT_LIBRARY_ENV_VAR = "RCLONE_KIT_LIBRARY"


def resolve_library_path(explicit_path: Path | None = None) -> Path:
    """Resolve an absolute path to the native `librclone_kit` library.

    Raises `LibraryNotFoundError` when `explicit_path` is given but does not
    exist, when the `RCLONE_KIT_LIBRARY` environment variable is set but
    does not point at an existing file, or when neither is set (no packaged
    asset resolver exists yet).
    """
    if explicit_path is not None:
        resolved = explicit_path.resolve()
        if not resolved.is_file():
            raise LibraryNotFoundError(resolved)
        return resolved

    env_value = os.environ.get(RCLONE_KIT_LIBRARY_ENV_VAR)
    if env_value:
        resolved = Path(env_value).resolve()
        if not resolved.is_file():
            raise LibraryNotFoundError(resolved)
        return resolved

    raise LibraryNotFoundError(None)
