"""Native-backed test for `find_conf_file_embedded()`'s fallback path:
querying the embedded runtime's own default config location via
`_config_paths_via_embedded_runtime()` when neither `explicit_path` nor the
`RCLONE_CONFIG` environment variable is set (the precedence-only branches
are covered offline by `tests/unit/test_config_discovery.py`).

Run in a fresh child process: `find_conf_file_embedded()` calls
`RcloneKitInitialize` itself (via a throwaway `RcloneRuntime`), which only
one caller per process may do, and this test process's `native_runtime`
session fixture has already claimed that slot.

Skipped automatically when no built native library exists (run
`scripts/native/build.py` first).
"""

import subprocess
import sys
import textwrap

import pytest
from conftest import LIBRARY_PATH, NATIVE_LIBRARY_AVAILABLE

pytestmark = pytest.mark.skipif(
    not NATIVE_LIBRARY_AVAILABLE,
    reason="No built native library found; run scripts/native/build.py first.",
)


def test_embedded_config_discovery_falls_back_to_the_runtime_query_in_a_clean_process() -> None:
    assert LIBRARY_PATH is not None
    script = textwrap.dedent(
        f"""
        from pathlib import Path
        from rclone_kit.config_discovery import find_conf_file_embedded

        result = find_conf_file_embedded(library_path=Path({str(LIBRARY_PATH)!r}))
        assert result is None or isinstance(result, Path), f"unexpected type: {{result!r}}"
        print(result)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    # A developer machine may or may not have a real default rclone.conf,
    # so the printed value can legitimately be "None" or a real path - the
    # in-script assert already proved the type; `check=True` above already
    # proved it completed without raising. This just confirms the script
    # printed exactly one line, not a traceback swallowed by `check=True`
    # succeeding on an empty run.
    assert completed.stdout.strip().splitlines() == [completed.stdout.strip()]
