"""Unit tests for the embedded RC-backed transfer operations (CLI-to-C-ABI
migration ledger rows T01, T02, T07).

Drives `_JobMonitor` with a fake `RcJobClient` (the same harness
`tests/unit/test_job.py` uses), so these tests exercise request mapping and
`check`/error semantics through the real async-job machinery without a
built native library. Native-DLL parity is covered by
`tests/native/test_transfer_ops_embedded_integration.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from rclone_kit.config import Config
from rclone_kit.exceptions import OperationFailedError, UnsupportedEmbeddedOperationError
from rclone_kit.job import _JobMonitor
from rclone_kit.operation import JobState, JobStatus, TransferStats
from rclone_kit.operations.transfer_ops_embedded import (
    cleanup_embedded,
    copy_file_to_embedded,
    purge_dir_embedded,
)
from rclone_kit.rc.jobs import RcJobRef

if TYPE_CHECKING:
    from collections.abc import Mapping

_POLL_INTERVAL = 0.02
_WAIT_TIMEOUT = 2.0
_NOW = datetime.now(UTC)
_CLIENT_ID = uuid.UUID(int=0)

_EMPTY_STATS = TransferStats(
    bytes=0,
    total_bytes=0,
    checks=0,
    total_checks=0,
    transfers=0,
    total_transfers=0,
    errors=0,
    fatal_error=False,
    retry_error=False,
    speed=0.0,
    eta_seconds=None,
    elapsed_seconds=0.0,
)

_S3_CONFIG_TEXT = """
[do-remote]
type = s3
provider = DigitalOcean
access_key_id = AKIAEXAMPLE
secret_access_key = super-secret
"""


def _empty_config() -> Config:
    return Config("")


class FakeJobClient:
    """A minimal fake `RcJobClient`: every started job settles immediately
    (on the first poll) with one canned success/failure outcome, mirroring
    `tests/unit/test_job.py`'s harness but simplified for these
    single-call, no-progress operations."""

    def __init__(self) -> None:
        self.starts: list[tuple[str, dict, str]] = []
        self._next_job_id = 1
        self.success = True
        self.error = ""

    def start(self, method: str, params: Mapping[str, object], group: str) -> RcJobRef:
        job_id = self._next_job_id
        self._next_job_id += 1
        self.starts.append((method, dict(params), group))
        return RcJobRef(job_id=job_id, execute_id=f"exec-{job_id}", group=group)

    def status(self, ref: RcJobRef) -> JobStatus:
        return JobStatus(
            job_id=ref.job_id,
            execute_id=ref.execute_id,
            group=ref.group,
            state=JobState.SUCCEEDED if self.success else JobState.FAILED,
            started_at=_NOW,
            ended_at=_NOW + timedelta(seconds=1),
            duration=1.0,
            error=None if self.success else (self.error or "boom"),
            output={},
        )

    def stats(self, group: str) -> TransferStats:  # noqa: ARG002
        return _EMPTY_STATS

    def stop(self, ref: RcJobRef) -> None:  # noqa: ARG002
        raise AssertionError("stop() should not be called for a job that already settled")

    def delete_stats(self, group: str) -> None:
        pass


def _monitor(job_client: FakeJobClient) -> _JobMonitor:
    return _JobMonitor(job_client, poll_interval_seconds=_POLL_INTERVAL, close_wait_seconds=2.0)


def test_copy_file_to_embedded_splits_parent_and_name_on_both_sides() -> None:
    job_client = FakeJobClient()

    copy_file_to_embedded(
        _monitor(job_client),
        _CLIENT_ID,
        _empty_config(),
        "remote:path/to/a.txt",
        "remote:other/b.txt",
    )

    assert len(job_client.starts) == 1
    method, params, _group = job_client.starts[0]
    assert method == "operations/copyfile"
    assert params == {
        "srcFs": "remote:path/to",
        "srcRemote": "a.txt",
        "dstFs": "remote:other",
        "dstRemote": "b.txt",
    }


def test_copy_file_to_embedded_encodes_an_s3_source_with_no_check_bucket() -> None:
    job_client = FakeJobClient()

    copy_file_to_embedded(
        _monitor(job_client),
        _CLIENT_ID,
        Config(_S3_CONFIG_TEXT),
        "do-remote:bucket/a.txt",
        "remote:other/b.txt",
    )

    _method, params, _group = job_client.starts[0]
    assert params == {
        "srcFs": {
            "_name": "do-remote",
            "_root": "bucket",
            "no_check_bucket": "true",
        },
        "srcRemote": "a.txt",
        "dstFs": "remote:other",
        "dstRemote": "b.txt",
    }


def test_copy_file_to_embedded_returns_ok_result_on_success() -> None:
    job_client = FakeJobClient()

    result = copy_file_to_embedded(
        _monitor(job_client), _CLIENT_ID, _empty_config(), "remote:a.txt", "remote:b.txt"
    )

    assert result.ok is True


def test_copy_file_to_embedded_raises_operation_failed_error_by_default_on_failure() -> None:
    job_client = FakeJobClient()
    job_client.success = False

    with pytest.raises(OperationFailedError):
        copy_file_to_embedded(
            _monitor(job_client), _CLIENT_ID, _empty_config(), "remote:a.txt", "remote:b.txt"
        )


def test_copy_file_to_embedded_wraps_failure_when_check_is_false() -> None:
    job_client = FakeJobClient()
    job_client.success = False

    result = copy_file_to_embedded(
        _monitor(job_client),
        _CLIENT_ID,
        _empty_config(),
        "remote:a.txt",
        "remote:b.txt",
        check=False,
    )

    assert result.ok is False


def test_copy_file_to_embedded_rejects_other_args() -> None:
    job_client = FakeJobClient()

    with pytest.raises(UnsupportedEmbeddedOperationError):
        copy_file_to_embedded(
            _monitor(job_client),
            _CLIENT_ID,
            _empty_config(),
            "remote:a.txt",
            "remote:b.txt",
            other_args=["--foo"],
        )

    assert job_client.starts == []


def test_purge_dir_embedded_uses_whole_target_split() -> None:
    job_client = FakeJobClient()

    purge_dir_embedded(_monitor(job_client), _CLIENT_ID, "remote:path/to/dir")

    method, params, _group = job_client.starts[0]
    assert method == "operations/purge"
    assert params == {"fs": "remote:", "remote": "path/to/dir"}


def test_purge_dir_embedded_never_raises_on_failure() -> None:
    job_client = FakeJobClient()
    job_client.success = False

    result = purge_dir_embedded(_monitor(job_client), _CLIENT_ID, "remote:path/to/dir")

    assert result.ok is False


def test_purge_dir_embedded_ok_on_success() -> None:
    job_client = FakeJobClient()

    result = purge_dir_embedded(_monitor(job_client), _CLIENT_ID, "remote:path/to/dir")

    assert result.ok is True


def test_cleanup_embedded_passes_fs_only() -> None:
    job_client = FakeJobClient()

    cleanup_embedded(_monitor(job_client), _CLIENT_ID, "remote:")

    method, params, _group = job_client.starts[0]
    assert method == "operations/cleanup"
    assert params == {"fs": "remote:"}


def test_cleanup_embedded_never_raises_on_failure() -> None:
    job_client = FakeJobClient()
    job_client.success = False

    result = cleanup_embedded(_monitor(job_client), _CLIENT_ID, "remote:")

    assert result.ok is False
