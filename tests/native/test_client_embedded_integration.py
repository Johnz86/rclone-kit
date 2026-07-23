"""Native-backed parity check for `Rclone(execution="embedded")`'s ported
`obscure()` (ledger row M01) and the embedded config-path discovery helper
(ledger row C03), against the CLI backend built from the exact same commit.

`obscure()` uses a random IV per call, so two obscured strings for the same
plaintext are never byte-identical - the real parity check is a round trip:
obscure through the embedded runtime, reveal through the same-commit CLI
executable, and confirm the original plaintext comes back.

The obscure test reuses the shared, already-initialized `native_runtime`
session fixture (see `conftest.py`); the config-discovery test spawns its own
child process instead, since it exercises `RcloneKitInitialize` itself, which
this process's `native_runtime` fixture has already consumed for the whole
test session.

Skipped automatically when no built native target exists (run
`scripts/native/build.py` first).
"""

import subprocess
import sys
import textwrap

import pytest

from conftest import EXECUTABLE_PATH, LIBRARY_PATH, NATIVE_EXECUTABLE_AVAILABLE
from rclone_kit.client import Rclone
from rclone_kit.native.runtime import RcloneRuntime

pytestmark = pytest.mark.skipif(
    not NATIVE_EXECUTABLE_AVAILABLE,
    reason="No built native executable found; run scripts/native/build.py first.",
)


def _reveal_via_cli(obscured: str) -> str:
    assert EXECUTABLE_PATH is not None
    completed = subprocess.run(
        [str(EXECUTABLE_PATH), "reveal", obscured],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


@pytest.mark.parametrize("password", ["hunter2", "", "Unicode-Résumé-密码"])
def test_embedded_obscure_round_trips_through_cli_reveal(
    password: str, native_runtime: RcloneRuntime
) -> None:
    embedded = Rclone(None, execution="embedded", runtime=native_runtime)

    obscured = embedded.obscure(password)

    assert obscured != password or password == ""
    assert _reveal_via_cli(obscured) == password


def test_embedded_config_discovery_matches_cli_default_in_a_clean_process() -> None:
    """Run in a fresh child process: `find_conf_file_embedded` calls
    `RcloneKitInitialize` itself, which only one caller per process may do,
    and this test process's `native_runtime` fixture has already claimed
    that for the whole session.
    """
    assert LIBRARY_PATH is not None
    assert EXECUTABLE_PATH is not None
    script = textwrap.dedent(
        f"""
        from pathlib import Path
        from rclone_kit.config_discovery import find_conf_file, find_conf_file_embedded

        embedded = find_conf_file_embedded(library_path=Path({str(LIBRARY_PATH)!r}))
        cli = find_conf_file(rclone_exe=Path({str(EXECUTABLE_PATH)!r}))
        print(embedded)
        print(cli)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    embedded_line, cli_line = completed.stdout.strip().splitlines()

    # A developer machine may have a real default rclone.conf; only assert
    # the two discovery paths agree, whichever way they come out.
    assert embedded_line == cli_line
