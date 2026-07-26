"""Integration test for `rclone_kit.native`/`rclone_kit.rc` against a real,
already-built `librclone_kit` shared library.

Skipped automatically when `build/native/<target>/` does not exist (run
`uv run python scripts/native/build.py --target <target>` first) — this test
is not part of the toolchain-free unit suite and never invokes Go/GCC
itself. `tests/unit/test_native_runtime.py` covers the same lifecycle logic
against a fake binding and always runs.
"""

import os
from pathlib import Path

import psutil
import pytest

from conftest import NATIVE_LIBRARY_AVAILABLE
from rclone_kit.native.runtime import RcloneRuntime
from rclone_kit.rc.client import RcClient
from rclone_kit.rc.errors import RcCallError

_MEMORY_BUCKET_REMOTE = "rc-client-bucket/hello.txt"
_MEMORY_TEST_CONTENT = b"rclone-kit native runtime integration test\n"

pytestmark = pytest.mark.skipif(
    not NATIVE_LIBRARY_AVAILABLE,
    reason="No built native library found; run scripts/native/build.py first.",
)


@pytest.fixture
def runtime(native_runtime: RcloneRuntime) -> RcloneRuntime:
    return native_runtime


def test_build_info_reports_expected_abi_version(runtime: RcloneRuntime) -> None:
    info = runtime.build_info()
    assert info.abi_version == 1


def test_rc_client_core_version(runtime: RcloneRuntime) -> None:
    client = RcClient(runtime)
    result = client.call("core/version")
    assert "version" in result


def test_rc_client_raises_on_unknown_method(runtime: RcloneRuntime) -> None:
    client = RcClient(runtime)
    with pytest.raises(RcCallError) as excinfo:
        client.call("not/a/real/method")
    assert excinfo.value.status == 404


def test_rc_client_memory_backend_crud(runtime: RcloneRuntime, tmp_path: Path) -> None:
    client = RcClient(runtime)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "hello.txt").write_bytes(_MEMORY_TEST_CONTENT)

    client.call(
        "operations/copyfile",
        srcFs=str(src_dir),
        srcRemote="hello.txt",
        dstFs=":memory:",
        dstRemote=_MEMORY_BUCKET_REMOTE,
    )

    listing = client.call("operations/list", fs=":memory:", remote="rc-client-bucket")
    names = [item["Name"] for item in listing.get("list", [])]
    assert "hello.txt" in names

    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()
    client.call(
        "operations/copyfile",
        srcFs=":memory:",
        srcRemote=_MEMORY_BUCKET_REMOTE,
        dstFs=str(dst_dir),
        dstRemote="hello.txt",
    )
    assert (dst_dir / "hello.txt").read_bytes() == _MEMORY_TEST_CONTENT

    client.call("operations/deletefile", fs=":memory:", remote=_MEMORY_BUCKET_REMOTE)
    listing = client.call("operations/list", fs=":memory:", remote="rc-client-bucket")
    assert listing.get("list", []) == []


def test_no_child_process_spawned(runtime: RcloneRuntime) -> None:
    client = RcClient(runtime)
    proc = psutil.Process(os.getpid())
    before = proc.children(recursive=True)
    client.call("core/version")
    after = proc.children(recursive=True)
    assert before == after
