from __future__ import annotations

import time
from pathlib import Path

from rclone_kit.backend import RcloneBackend
from rclone_kit.command_flags import FLAG_VFS_CACHE_MODE
from rclone_kit.convert import convert_to_str
from rclone_kit.dir import Dir
from rclone_kit.http_server import HttpServer
from rclone_kit.process import Process
from rclone_kit.remote import Remote
from rclone_kit.util import find_free_port


def launch_webdav_server(
    backend: RcloneBackend,
    src: Remote | Dir | str,
    user: str,
    password: str,
    addr: str = "localhost:2049",
    allow_other: bool = False,
    other_args: list[str] | None = None,
) -> Process:
    """Serve a remote or directory via NFS.

    Raises `ValueError` if the NFS server fails to start.
    """
    src_str = convert_to_str(src)
    cmd_list: list[str] = ["serve", "webdav", "--addr", addr, src_str]
    cmd_list.extend(["--user", user, "--pass", password])
    if allow_other:
        cmd_list.append("--allow-other")
    if other_args:
        cmd_list += other_args
    proc = backend.launch(tuple(cmd_list))
    time.sleep(2)
    if proc.poll() is not None:
        raise ValueError("NFS serve process failed to start")
    return proc


def launch_http_server(
    backend: RcloneBackend,
    src: str,
    cache_mode: str | None,
    addr: str | None = None,
    serve_http_log: Path | None = None,
    other_args: list[str] | None = None,
) -> HttpServer:
    """Serve a remote or directory via HTTP.

    Raises `ValueError` if the HTTP server fails to start.
    """
    addr = addr or f"localhost:{find_free_port()}"
    _, subpath = src.split(":", 1)
    cmd_list: list[str] = [
        "serve",
        "http",
        "--addr",
        addr,
        src,
        "--vfs-disk-space-total-size",
        "0",
        "--vfs-read-chunk-size-limit",
        "512M",
    ]

    if cache_mode:
        cmd_list += [
            FLAG_VFS_CACHE_MODE,
            cache_mode,
        ]
    if serve_http_log:
        cmd_list += ["--log-file", str(serve_http_log)]
        cmd_list += ["-vvvv"]
    if other_args:
        cmd_list += other_args
    proc = backend.launch(tuple(cmd_list), log=serve_http_log)
    time.sleep(2)
    if proc.poll() is not None:
        raise ValueError("HTTP serve process failed to start")
    out: HttpServer = HttpServer(url=f"http://{addr}", subpath=subpath, process=proc)
    return out
