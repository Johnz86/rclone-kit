"""Unit tests for `MountHandle`, driven by a fake `RcMountClient`."""

from pathlib import Path

from rclone_kit.mount_handle import MountHandle
from rclone_kit.rc.mount import MountRef


class FakeMountClient:
    def __init__(self) -> None:
        self.unmount_calls: list[str] = []
        self.raise_on_unmount = False

    def mount(self, fs: str, mount_point: str, **kwargs) -> MountRef:  # noqa: ARG002
        raise AssertionError("mount() should not be called by MountHandle itself")

    def unmount(self, mount_point: str) -> None:
        self.unmount_calls.append(mount_point)
        if self.raise_on_unmount:
            raise RuntimeError("boom")


def test_mount_path_exposes_the_mount_point() -> None:
    client = FakeMountClient()
    handle = MountHandle(client, "Z:")

    assert handle.mount_path == Path("Z:")


def test_dispose_calls_unmount_once() -> None:
    client = FakeMountClient()
    handle = MountHandle(client, "Z:")

    handle.dispose()

    assert client.unmount_calls == ["Z:"]
    assert handle.closed is True


def test_dispose_is_idempotent() -> None:
    client = FakeMountClient()
    handle = MountHandle(client, "Z:")

    handle.dispose()
    handle.dispose()

    assert client.unmount_calls == ["Z:"]


def test_dispose_swallows_unmount_failures() -> None:
    client = FakeMountClient()
    client.raise_on_unmount = True
    handle = MountHandle(client, "Z:")

    handle.dispose()  # must not raise

    assert handle.closed is True


def test_context_manager_disposes_on_exit() -> None:
    client = FakeMountClient()

    with MountHandle(client, "Z:") as handle:
        assert handle.closed is False

    assert handle.closed is True
    assert client.unmount_calls == ["Z:"]
