"""Native-backed parity check for the embedded RC-backed mount operations
(ledger rows R01 `mount`, R02 `mount_s3`), per the Wave H design
(`native_c_abi_wave_h_review_and_design.md`'s mount addendum).

Skipped automatically unless the currently built native target was
produced with `--profile production` (`-tags cmount` plus the installed
WinFsp SDK's `CPATH`): a `--profile development` build has no real
`mount/mount` implementation registered at all (see
`native/rclone/librclone/rclonekit/bridge/imports.go`).

Each test mounts to `mountPoint="*"` so rclone auto-assigns a free drive
letter, rather than a test picking one and racing every other test (or a
real drive already in use) for it.
"""

import time
from pathlib import Path

import pytest
from conftest import NATIVE_MOUNT_AVAILABLE

from rclone_kit.client import Rclone
from rclone_kit.mount_handle import MountHandle

pytestmark = pytest.mark.skipif(
    not NATIVE_MOUNT_AVAILABLE,
    reason="No built native target with mount support found; run "
    "scripts/native/build.py --profile production first.",
)

_MOUNT_SETTLE_SECONDS = 2
_UNMOUNT_SETTLE_SECONDS = 1


@pytest.fixture(autouse=True)
def _settle_between_mounts() -> None:
    """WinFsp needs a moment to fully release a drive letter after
    unmount before it can be reliably reassigned - without this, a test
    mounting right after a prior test's `dispose()` can see its mount
    torn down within the same second it was assigned."""
    yield
    time.sleep(_UNMOUNT_SETTLE_SECONDS)


def test_mount_reads_a_real_file(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "hello.txt").write_bytes(b"hello from embedded mount")

    handle = embedded.mount(str(src), Path("*"))
    try:
        assert isinstance(handle, MountHandle)
        time.sleep(_MOUNT_SETTLE_SECONDS)
        assert (handle.mount_path / "hello.txt").read_bytes() == b"hello from embedded mount"
    finally:
        handle.dispose()


def test_mount_allows_writes_when_read_only_cache_off(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    src.mkdir()

    handle = embedded.mount(str(src), Path("*"), allow_writes=True, vfs_cache_mode="off")
    try:
        time.sleep(_MOUNT_SETTLE_SECONDS)
        (handle.mount_path / "new.txt").write_bytes(b"written through mount")
        assert (handle.mount_path / "new.txt").read_bytes() == b"written through mount"
    finally:
        handle.dispose()
        time.sleep(_UNMOUNT_SETTLE_SECONDS)
    assert (src / "new.txt").read_bytes() == b"written through mount"


def test_mount_dispose_is_idempotent(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    src.mkdir()

    handle = embedded.mount(str(src), Path("*"))
    assert isinstance(handle, MountHandle)

    handle.dispose()
    handle.dispose()

    assert handle.closed is True


def test_close_disposes_mount_handles_the_caller_never_disposed(
    tmp_path: Path, embedded: Rclone
) -> None:
    src = tmp_path / "src"
    src.mkdir()

    handle = embedded.mount(str(src), Path("*"))
    assert isinstance(handle, MountHandle)
    assert handle.closed is False

    embedded.close()

    assert handle.closed is True


def test_mount_s3_reads_a_real_file(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "hello.txt").write_bytes(b"hello from s3 preset mount")

    handle = embedded.mount_s3(str(src), Path("*"))
    try:
        assert isinstance(handle, MountHandle)
        time.sleep(_MOUNT_SETTLE_SECONDS)
        assert (handle.mount_path / "hello.txt").read_bytes() == b"hello from s3 preset mount"
    finally:
        handle.dispose()
