"""Raw `ctypes` binding over the rclone-kit C ABI (`native/rclone/librclone/rclonekit/abi.h`).

This is the only module in `rclone_kit` allowed to import `ctypes`. Every
other native/rc module talks to `RcloneKitBinding` (or a fake implementing
the same shape, for unit tests) and never touches a raw pointer.

Every `Output`-shaped out-parameter must be declared `c_void_p`, never
`c_char_p`: a `c_char_p` argument or struct field is auto-converted to a
Python `bytes` object by `ctypes` on read, and freeing that converted
*copy's* address (instead of the real allocation) corrupts the heap.
`scripts/native/smoke.py` documents and exercises the same rule.
"""

import ctypes
from pathlib import Path


class RcloneKitBinding:
    """Owns one loaded `librclone_kit` shared library for the lifetime of
    this object. `ctypes.CDLL` cannot be safely unloaded; call `initialize`
    at most once and let process exit be the hard cleanup boundary, per
    `abi.h`'s documented contract.
    """

    def __init__(self, library_path: Path) -> None:
        self._lib = ctypes.CDLL(str(library_path))
        self._declare_signatures()

    def _declare_signatures(self) -> None:
        lib = self._lib

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

    def _take_output(self, out_ptr: ctypes.c_void_p, out_len: ctypes.c_size_t) -> bytes:
        ptr = out_ptr.value
        length = out_len.value
        if not ptr or length == 0:
            return b""
        try:
            return ctypes.string_at(ptr, length)
        finally:
            self._lib.RcloneKitFree(ptr)

    def _call_no_input(self, fn: "ctypes._FuncPointer") -> tuple[int, bytes]:
        out_ptr = ctypes.c_void_p()
        out_len = ctypes.c_size_t()
        status = fn(ctypes.byref(out_ptr), ctypes.byref(out_len))
        return status, self._take_output(out_ptr, out_len)

    def abi_version(self) -> int:
        return self._lib.RcloneKitABIVersion()

    def build_info(self) -> tuple[int, bytes]:
        return self._call_no_input(self._lib.RcloneKitBuildInfo)

    def initialize(self, payload: bytes) -> tuple[int, bytes]:
        out_ptr = ctypes.c_void_p()
        out_len = ctypes.c_size_t()
        status = self._lib.RcloneKitInitialize(
            payload if payload else None,
            len(payload),
            ctypes.byref(out_ptr),
            ctypes.byref(out_len),
        )
        return status, self._take_output(out_ptr, out_len)

    def rpc(self, method: bytes, payload: bytes) -> tuple[int, bytes]:
        out_ptr = ctypes.c_void_p()
        out_len = ctypes.c_size_t()
        status = self._lib.RcloneKitRPC(
            method,
            len(method),
            payload if payload else None,
            len(payload),
            ctypes.byref(out_ptr),
            ctypes.byref(out_len),
        )
        return status, self._take_output(out_ptr, out_len)

    def finalize(self) -> tuple[int, bytes]:
        return self._call_no_input(self._lib.RcloneKitFinalize)
