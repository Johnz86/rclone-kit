"""Native C ABI smoke test for one built `librclone_kit` shared library.

Exercises every proof point a working native library must satisfy directly
through `ctypes`, without importing any `rclone_kit` Python runtime layer -
this keeps the smoke test valid evidence of the raw ABI contract,
independent of the Python wrapper built on top of it: ABI version, build
info, fixed-config initialization, repeated RPC calls, `rc/list`, a
memory-backend CRUD round trip, an unknown-method 404, allocation/free
correctness, and no spawned child process.

`scripts/native/build.py` is the canonical caller: it runs this against every
freshly built library and writes the result as `smoke-results.json` inside
the build output directory. Can also be run standalone:

    uv run python scripts/native/smoke.py build/native/windows-amd64/librclone_kit.dll
"""

import ctypes
import json
import os
import sys
import tempfile
from pathlib import Path

import psutil

_CORE_VERSION_CALL_COUNT = 5
_MEMORY_BUCKET_REMOTE = "smoke-bucket/hello.txt"
_MEMORY_TEST_CONTENT = b"rclone-kit native smoke test\n"
_UNKNOWN_METHOD_STATUS = 404
_OK_STATUS = 200
_ALREADY_INITIALIZED_STATUS = -3


class NativeSmokeTestError(Exception):
    """Raised when a built library fails any smoke-test assertion."""


def _declare_signatures(lib: ctypes.CDLL) -> None:
    lib.RcloneKitABIVersion.argtypes = []
    lib.RcloneKitABIVersion.restype = ctypes.c_uint32

    lib.RcloneKitBuildInfo.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    lib.RcloneKitBuildInfo.restype = ctypes.c_int32

    lib.RcloneKitInitialize.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    lib.RcloneKitInitialize.restype = ctypes.c_int32

    lib.RcloneKitRPC.argtypes = [
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    lib.RcloneKitRPC.restype = ctypes.c_int32

    lib.RcloneKitFinalize.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    lib.RcloneKitFinalize.restype = ctypes.c_int32

    lib.RcloneKitFree.argtypes = [ctypes.c_void_p]
    lib.RcloneKitFree.restype = None


def _take_output(lib: ctypes.CDLL, out_ptr: ctypes.c_void_p, out_len: ctypes.c_size_t) -> dict:
    """Decode and free one `output`/`output_length` allocation.

    `out_ptr` must be declared `c_void_p`, never `c_char_p`: a `c_char_p`
    struct/out-param is auto-converted to a Python `bytes` object on read,
    and freeing that converted copy's (wrong) address corrupts the heap.
    """
    ptr = out_ptr.value
    length = out_len.value
    if not ptr or length == 0:
        return {}
    try:
        data = ctypes.string_at(ptr, length)
        return json.loads(data.decode("utf-8"))
    finally:
        lib.RcloneKitFree(ptr)


def _call_no_input(lib: ctypes.CDLL, fn: "ctypes._FuncPointer") -> tuple[int, dict]:
    out_ptr = ctypes.c_void_p()
    out_len = ctypes.c_size_t()
    status = fn(ctypes.byref(out_ptr), ctypes.byref(out_len))
    return status, _take_output(lib, out_ptr, out_len)


def _rpc(lib: ctypes.CDLL, method: bytes, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    out_ptr = ctypes.c_void_p()
    out_len = ctypes.c_size_t()
    status = lib.RcloneKitRPC(
        method, len(method), body, len(body), ctypes.byref(out_ptr), ctypes.byref(out_len)
    )
    return status, _take_output(lib, out_ptr, out_len)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise NativeSmokeTestError(message)


def _check_memory_backend_crud(lib: ctypes.CDLL) -> None:
    """Round-trip a file through the `:memory:` backend.

    Uses a nested `bucket/file` path: the memory backend has bucket-style
    (S3-like) semantics, so a bare top-level name addresses a bucket, not a
    file.
    """
    src_dir = Path(tempfile.mkdtemp())
    src_file = src_dir / "hello.txt"
    src_file.write_bytes(_MEMORY_TEST_CONTENT)

    status, out = _rpc(
        lib,
        b"operations/copyfile",
        {
            "srcFs": str(src_dir),
            "srcRemote": "hello.txt",
            "dstFs": ":memory:",
            "dstRemote": _MEMORY_BUCKET_REMOTE,
        },
    )
    _require(status == _OK_STATUS, f"copyfile (create) failed: {status} {out}")

    status, out = _rpc(lib, b"operations/list", {"fs": ":memory:", "remote": "smoke-bucket"})
    _require(status == _OK_STATUS, f"list after create failed: {status} {out}")
    names = [item["Name"] for item in out.get("list", [])]
    _require("hello.txt" in names, f"listing did not show created object: {out}")

    dst_dir = Path(tempfile.mkdtemp())
    status, out = _rpc(
        lib,
        b"operations/copyfile",
        {
            "srcFs": ":memory:",
            "srcRemote": _MEMORY_BUCKET_REMOTE,
            "dstFs": str(dst_dir),
            "dstRemote": "hello.txt",
        },
    )
    _require(status == _OK_STATUS, f"copyfile (read) failed: {status} {out}")
    _require(
        (dst_dir / "hello.txt").read_bytes() == _MEMORY_TEST_CONTENT,
        "round-tripped content mismatch",
    )

    status, out = _rpc(
        lib, b"operations/deletefile", {"fs": ":memory:", "remote": _MEMORY_BUCKET_REMOTE}
    )
    _require(status == _OK_STATUS, f"deletefile failed: {status} {out}")

    status, out = _rpc(lib, b"operations/list", {"fs": ":memory:", "remote": "smoke-bucket"})
    _require(status == _OK_STATUS, f"list after delete failed: {status} {out}")
    _require(out.get("list", []) == [], f"object still listed after delete: {out}")


def run_smoke_test(library_path: Path) -> dict:
    """Load `library_path` and run every native smoke-test assertion.

    Returns a JSON-serializable result summary on success. Raises
    `NativeSmokeTestError` on the first failed assertion; the caller is
    responsible for deciding whether a failed smoke test should fail the
    overall native build.
    """
    lib = ctypes.CDLL(str(library_path))
    _declare_signatures(lib)

    abi_version = lib.RcloneKitABIVersion()

    proc = psutil.Process(os.getpid())
    children_before = proc.children(recursive=True)

    status, build_info = _call_no_input(lib, lib.RcloneKitBuildInfo)
    _require(status == 0, f"BuildInfo before init failed: {status} {build_info}")

    config_path = Path(tempfile.mkdtemp()) / "rclone.conf"
    init_payload = json.dumps({"configPath": str(config_path)}).encode("utf-8")
    out_ptr = ctypes.c_void_p()
    out_len = ctypes.c_size_t()
    status = lib.RcloneKitInitialize(
        init_payload, len(init_payload), ctypes.byref(out_ptr), ctypes.byref(out_len)
    )
    init_info = _take_output(lib, out_ptr, out_len)
    _require(status == 0, f"Initialize failed: {status} {init_info}")

    out_ptr = ctypes.c_void_p()
    out_len = ctypes.c_size_t()
    status = lib.RcloneKitInitialize(None, 0, ctypes.byref(out_ptr), ctypes.byref(out_len))
    second_init_info = _take_output(lib, out_ptr, out_len)
    _require(
        status == _ALREADY_INITIALIZED_STATUS,
        f"expected StatusAlreadyInitialized(-3) on second Initialize, got {status} {second_init_info}",
    )

    last_version_response: dict = {}
    for _ in range(_CORE_VERSION_CALL_COUNT):
        status, last_version_response = _rpc(lib, b"core/version", {})
        _require(
            status == _OK_STATUS, f"core/version call failed: {status} {last_version_response}"
        )
    _require(
        "version" in last_version_response,
        f"core/version missing 'version': {last_version_response}",
    )

    status, list_response = _rpc(lib, b"rc/list", {})
    _require(status == _OK_STATUS, f"rc/list failed: {status} {list_response}")
    registered_paths = {command["Path"] for command in list_response.get("commands", [])}
    _require(
        {"core/version", "operations/list"} <= registered_paths,
        f"rc/list missing expected methods: {sorted(registered_paths)}",
    )

    _check_memory_backend_crud(lib)

    status, unknown_response = _rpc(lib, b"not/a/real/method", {})
    _require(
        status == _UNKNOWN_METHOD_STATUS,
        f"expected {_UNKNOWN_METHOD_STATUS} for an unknown method, got {status} {unknown_response}",
    )

    children_after = proc.children(recursive=True)
    _require(
        children_after == children_before,
        f"embedded RPC calls must not spawn child processes: "
        f"before={children_before} after={children_after}",
    )

    status, finalize_info = _call_no_input(lib, lib.RcloneKitFinalize)
    _require(status == 0, f"Finalize failed: {status} {finalize_info}")

    return {
        "abiVersion": abi_version,
        "buildInfo": build_info,
        "registeredRcMethodCount": len(registered_paths),
        "coreVersionCallCount": _CORE_VERSION_CALL_COUNT,
        "childProcessesSpawned": 0,
        "passed": True,
    }


def main(argv: list[str] | None = None) -> int:
    """Run the native smoke test against a library path given on argv[0].

    Returns 0 and prints the JSON result summary on success. Returns 1 and
    prints a diagnostic to stderr when any assertion fails.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Usage: smoke.py <path-to-librclone_kit.dll-or-.so>", file=sys.stderr)
        return 1
    library_path = Path(args[0])
    try:
        result = run_smoke_test(library_path)
    except NativeSmokeTestError as error:
        print(f"Native smoke test failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
