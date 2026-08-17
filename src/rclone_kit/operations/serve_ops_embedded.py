"""Embedded RC-backed serve operations: `fetch_serve_webdav_embedded`/
`fetch_serve_http_embedded`.

Both start a `serve/start` instance via `RcServeClient` and wrap the
result in a `ServeHandle` - `serve_http()` further wraps that `ServeHandle`
inside an `HttpServer`, which needs only "is it still alive" and "dispose
it" from whatever it is given - see `http_server.py`'s
`_DisposableServerHandle` protocol.

The actual bound address always comes from `serve/start`'s own response,
never a Python-side free-port guess: requesting port `0` lets
rclone bind an ephemeral port and report back what it actually chose,
avoiding the inherent TOCTOU race a separate "find a free port, hope
rclone binds it" step would have.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rclone_kit.http_server import HttpServer
from rclone_kit.serve_handle import ServeHandle

if TYPE_CHECKING:
    from rclone_kit.rc.serve import RcServeClient

_VFS_DISK_SPACE_TOTAL_SIZE = "0"
_VFS_READ_CHUNK_SIZE_LIMIT = "512M"


def fetch_serve_http_embedded(
    serve_client: RcServeClient,
    src: str,
    cache_mode: str | None,
    addr: str | None = None,
) -> HttpServer:
    """Serve a remote or directory via HTTP through `serve/start type=http`.

    Sends `vfs_disk_space_total_size` and `vfs_read_chunk_size_limit` as
    RC parameters so a served remote reports unlimited space and streams
    range reads in bounded chunks.
    """
    # `partition`, not `split(":", 1)` - a colonless local path (most
    # commonly a Unix absolute path) has no subpath at all, and unpacking
    # `split`'s single-element result would raise ValueError before ever
    # reaching the RC call below.
    _, _, subpath = src.partition(":")
    params: dict[str, object] = {
        "vfs_disk_space_total_size": _VFS_DISK_SPACE_TOTAL_SIZE,
        "vfs_read_chunk_size_limit": _VFS_READ_CHUNK_SIZE_LIMIT,
    }
    if cache_mode:
        params["vfs_cache_mode"] = cache_mode
    ref = serve_client.start("http", src, addr or "127.0.0.1:0", params)
    handle = ServeHandle(serve_client, ref)
    return HttpServer(url=f"http://{ref.addr}", subpath=subpath, process=handle)


def fetch_serve_webdav_embedded(
    serve_client: RcServeClient,
    src: str,
    user: str,
    password: str,
    addr: str,
    allow_other: bool = False,
) -> ServeHandle:
    """Serve a remote or directory via WebDAV through `serve/start
    type=webdav`."""
    params: dict[str, object] = {"user": user, "pass": password}
    if allow_other:
        params["allow_other"] = True
    ref = serve_client.start("webdav", src, addr, params)
    return ServeHandle(serve_client, ref)
