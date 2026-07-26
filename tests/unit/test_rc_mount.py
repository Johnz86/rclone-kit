"""Unit tests for `rclone_kit.rc.mount`'s RC mount boundary
(`RcloneRcMountClient`): request mapping and strict response parsing.

Uses a fake `RcCallable` driven by canned per-method responses, so these
tests exercise request/response mapping without a built native library.
The shapes asserted here were captured from ad hoc probes against the
real built library during development (see the module docstring in
`rc/mount.py`).
"""

import pytest

from rclone_kit.rc.mount import MountRef, RcloneRcMountClient


class FakeRcClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._queues: dict[str, list[dict]] = {}

    def queue(self, method: str, *responses: dict) -> None:
        self._queues[method] = list(responses)

    def call(self, method: str, **params: object) -> dict:
        self.calls.append((method, params))
        queue = self._queues.get(method)
        if not queue:
            raise AssertionError(f"no queued response for {method!r}")
        return queue[0] if len(queue) == 1 else queue.pop(0)


class TestMount:
    def test_mount_sends_fs_and_mount_point(self) -> None:
        client = FakeRcClient()
        client.queue("mount/mount", {"mountPoint": "/mnt/tmp"})
        mount_client = RcloneRcMountClient(client)

        ref = mount_client.mount("remote:base", "/mnt/tmp")

        assert ref == MountRef(mount_point="/mnt/tmp")
        assert client.calls == [("mount/mount", {"fs": "remote:base", "mountPoint": "/mnt/tmp"})]

    def test_mount_sends_vfs_opt_and_mount_opt_when_given(self) -> None:
        client = FakeRcClient()
        client.queue("mount/mount", {"mountPoint": "/mnt/tmp"})
        mount_client = RcloneRcMountClient(client)

        mount_client.mount(
            "remote:base",
            "/mnt/tmp",
            vfs_opt={"ReadOnly": True, "CacheMode": "full"},
            mount_opt={"AttrTimeout": "1h"},
        )

        assert client.calls == [
            (
                "mount/mount",
                {
                    "fs": "remote:base",
                    "mountPoint": "/mnt/tmp",
                    "vfsOpt": {"ReadOnly": True, "CacheMode": "full"},
                    "mountOpt": {"AttrTimeout": "1h"},
                },
            )
        ]

    def test_mount_sends_flat_config_params(self) -> None:
        client = FakeRcClient()
        client.queue("mount/mount", {"mountPoint": "/mnt/tmp"})
        mount_client = RcloneRcMountClient(client)

        mount_client.mount("remote:base", "/mnt/tmp", config={"transfers": 8})

        assert client.calls == [
            ("mount/mount", {"fs": "remote:base", "mountPoint": "/mnt/tmp", "transfers": 8})
        ]

    def test_mount_returned_point_may_differ_from_requested(self) -> None:
        client = FakeRcClient()
        client.queue("mount/mount", {"mountPoint": "Z:"})
        mount_client = RcloneRcMountClient(client)

        ref = mount_client.mount("remote:base", "*")

        assert ref.mount_point == "Z:"

    def test_mount_non_string_mount_point_is_rejected(self) -> None:
        client = FakeRcClient()
        client.queue("mount/mount", {"mountPoint": 1})
        mount_client = RcloneRcMountClient(client)

        with pytest.raises(ValueError, match="mountPoint"):
            mount_client.mount("remote:base", "/mnt/tmp")


class TestUnmount:
    def test_unmount_sends_mount_point(self) -> None:
        client = FakeRcClient()
        client.queue("mount/unmount", {})
        mount_client = RcloneRcMountClient(client)

        mount_client.unmount("/mnt/tmp")

        assert client.calls == [("mount/unmount", {"mountPoint": "/mnt/tmp"})]
