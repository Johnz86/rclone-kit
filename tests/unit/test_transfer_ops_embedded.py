"""Unit tests for the embedded RC-backed transfer operations (CLI-to-C-ABI
migration ledger rows T01, T02, T07).

Uses a fake `RcClient`-shaped object driven by canned per-method responses
or exceptions, so these tests exercise request mapping and `check`/error
semantics without a built native library. Native-DLL parity is covered by
`tests/native/test_transfer_ops_embedded_integration.py`.
"""

import pytest

from rclone_kit.config import Config
from rclone_kit.exceptions import UnsupportedEmbeddedOperationError
from rclone_kit.operations.transfer_ops_embedded import (
    cleanup_embedded,
    copy_file_to_embedded,
    purge_dir_embedded,
)
from rclone_kit.rc.errors import RcCallError

_S3_CONFIG_TEXT = """
[do-remote]
type = s3
provider = DigitalOcean
access_key_id = AKIAEXAMPLE
secret_access_key = super-secret
"""


def _empty_config() -> Config:
    return Config("")


class FakeRcClient:
    """A fake `RcClient` driven by one canned response or exception per RC
    method.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, dict] = {}
        self.errors: dict[str, Exception] = {}

    def call(self, method: str, **params: object) -> dict:
        self.calls.append((method, params))
        if method in self.errors:
            raise self.errors[method]
        return self.responses.get(method, {})


def test_copy_file_to_embedded_splits_parent_and_name_on_both_sides() -> None:
    client = FakeRcClient()

    copy_file_to_embedded(client, _empty_config(), "remote:path/to/a.txt", "remote:other/b.txt")

    assert client.calls == [
        (
            "operations/copyfile",
            {
                "srcFs": "remote:path/to",
                "srcRemote": "a.txt",
                "dstFs": "remote:other",
                "dstRemote": "b.txt",
            },
        )
    ]


def test_copy_file_to_embedded_encodes_an_s3_source_with_no_check_bucket() -> None:
    client = FakeRcClient()

    copy_file_to_embedded(
        client, Config(_S3_CONFIG_TEXT), "do-remote:bucket/a.txt", "remote:other/b.txt"
    )

    assert client.calls == [
        (
            "operations/copyfile",
            {
                "srcFs": {
                    "_name": "do-remote",
                    "_root": "bucket",
                    "no_check_bucket": "true",
                },
                "srcRemote": "a.txt",
                "dstFs": "remote:other",
                "dstRemote": "b.txt",
            },
        )
    ]


def test_copy_file_to_embedded_returns_ok_completed_process_on_success() -> None:
    client = FakeRcClient()

    result = copy_file_to_embedded(client, _empty_config(), "remote:a.txt", "remote:b.txt")

    assert result.ok is True
    assert result.returncode == 0


def test_copy_file_to_embedded_raises_by_default_on_failure() -> None:
    client = FakeRcClient()
    client.errors["operations/copyfile"] = RcCallError("operations/copyfile", 500, {})

    with pytest.raises(RcCallError):
        copy_file_to_embedded(client, _empty_config(), "remote:a.txt", "remote:b.txt")


def test_copy_file_to_embedded_wraps_failure_when_check_is_false() -> None:
    client = FakeRcClient()
    client.errors["operations/copyfile"] = RcCallError("operations/copyfile", 500, {})

    result = copy_file_to_embedded(
        client, _empty_config(), "remote:a.txt", "remote:b.txt", check=False
    )

    assert result.ok is False
    assert result.returncode != 0


def test_copy_file_to_embedded_rejects_other_args() -> None:
    client = FakeRcClient()

    with pytest.raises(UnsupportedEmbeddedOperationError):
        copy_file_to_embedded(
            client, _empty_config(), "remote:a.txt", "remote:b.txt", other_args=["--foo"]
        )

    assert client.calls == []


def test_purge_dir_embedded_uses_whole_target_split() -> None:
    client = FakeRcClient()

    purge_dir_embedded(client, "remote:path/to/dir")

    assert client.calls == [("operations/purge", {"fs": "remote:", "remote": "path/to/dir"})]


def test_purge_dir_embedded_never_raises_on_failure() -> None:
    client = FakeRcClient()
    client.errors["operations/purge"] = RcCallError("operations/purge", 500, {})

    result = purge_dir_embedded(client, "remote:path/to/dir")

    assert result.ok is False


def test_purge_dir_embedded_ok_on_success() -> None:
    client = FakeRcClient()

    result = purge_dir_embedded(client, "remote:path/to/dir")

    assert result.ok is True


def test_cleanup_embedded_passes_fs_only() -> None:
    client = FakeRcClient()

    cleanup_embedded(client, "remote:")

    assert client.calls == [("operations/cleanup", {"fs": "remote:"})]


def test_cleanup_embedded_never_raises_on_failure() -> None:
    client = FakeRcClient()
    client.errors["operations/cleanup"] = RcCallError("operations/cleanup", 500, {})

    result = cleanup_embedded(client, "remote:")

    assert result.ok is False
