"""Unit tests for the embedded RC-backed serve operations (`serve_webdav`,
`serve_http`), driven by a fake `RcServeClient`. Native-DLL parity is
covered by `tests/native/test_serve_ops_embedded_integration.py`.
"""

from rclone_kit.operations.serve_ops_embedded import (
    fetch_serve_http_embedded,
    fetch_serve_webdav_embedded,
)
from rclone_kit.rc.serve import ServeRef
from rclone_kit.serve_handle import ServeHandle


class FakeServeClient:
    def __init__(self) -> None:
        self.start_calls: list[tuple[str, str, str, dict]] = []
        self.next_id = 1

    def start(self, serve_type: str, fs: str, addr: str, params) -> ServeRef:
        self.start_calls.append((serve_type, fs, addr, dict(params)))
        ref = ServeRef(id=f"{serve_type}-{self.next_id}", addr="127.0.0.1:12345")
        self.next_id += 1
        return ref

    def stop(self, ref_id: str) -> None:
        del ref_id

    def list(self) -> tuple[ServeRef, ...]:
        return ()


def test_fetch_serve_http_embedded_sends_type_http_and_default_vfs_flags() -> None:
    client = FakeServeClient()

    server = fetch_serve_http_embedded(client, "remote:base", cache_mode=None)

    assert client.start_calls == [
        (
            "http",
            "remote:base",
            "127.0.0.1:0",
            {"vfs_disk_space_total_size": "0", "vfs_read_chunk_size_limit": "512M"},
        )
    ]
    assert server.url == "http://127.0.0.1:12345"
    assert server.subpath == "base"


def test_fetch_serve_http_embedded_accepts_a_colonless_local_path() -> None:
    # A bare Unix-style local path (no remote prefix, so no colon at all)
    # must not raise ValueError trying to split off a subpath that doesn't
    # exist - see finding #6's "Linux path-modeling bugs".
    client = FakeServeClient()

    server = fetch_serve_http_embedded(client, "/srv/data", cache_mode=None)

    assert client.start_calls[0][1] == "/srv/data"
    assert server.subpath == ""


def test_fetch_serve_http_embedded_uses_the_requested_addr() -> None:
    client = FakeServeClient()

    fetch_serve_http_embedded(client, "remote:base", cache_mode=None, addr="localhost:8080")

    _type, _fs, addr, _params = client.start_calls[0]
    assert addr == "localhost:8080"


def test_fetch_serve_http_embedded_includes_cache_mode_when_set() -> None:
    client = FakeServeClient()

    fetch_serve_http_embedded(client, "remote:base", cache_mode="full")

    _type, _fs, _addr, params = client.start_calls[0]
    assert params["vfs_cache_mode"] == "full"


def test_fetch_serve_http_embedded_process_is_a_serve_handle() -> None:
    client = FakeServeClient()

    server = fetch_serve_http_embedded(client, "remote:base", cache_mode=None)

    assert isinstance(server.process, ServeHandle)
    assert server.process.id == "http-1"


def test_fetch_serve_webdav_embedded_sends_type_webdav_and_credentials() -> None:
    client = FakeServeClient()

    handle = fetch_serve_webdav_embedded(
        client, "remote:base", "alice", "hunter2", "127.0.0.1:2049"
    )

    assert client.start_calls == [
        ("webdav", "remote:base", "127.0.0.1:2049", {"user": "alice", "pass": "hunter2"})
    ]
    assert handle.id == "webdav-1"


def test_fetch_serve_webdav_embedded_allow_other_sets_the_flag() -> None:
    client = FakeServeClient()

    fetch_serve_webdav_embedded(
        client, "remote:base", "alice", "hunter2", "127.0.0.1:2049", allow_other=True
    )

    _type, _fs, _addr, params = client.start_calls[0]
    assert params["allow_other"] is True


def test_fetch_serve_webdav_embedded_omits_allow_other_by_default() -> None:
    client = FakeServeClient()

    fetch_serve_webdav_embedded(client, "remote:base", "alice", "hunter2", "127.0.0.1:2049")

    _type, _fs, _addr, params = client.start_calls[0]
    assert "allow_other" not in params
