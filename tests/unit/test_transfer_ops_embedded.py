"""Unit tests for the embedded RC-backed transfer operations (CLI-to-C-ABI
migration ledger rows T01, T02, T06, T07, T08).

Drives `_JobMonitor` with a fake `RcJobClient` (the same harness
`tests/unit/test_job.py` uses), so these tests exercise request mapping and
`check`/error semantics through the real async-job machinery without a
built native library. Native-DLL parity is covered by
`tests/native/test_transfer_ops_embedded_integration.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from rclone_kit.config import Config
from rclone_kit.exceptions import OperationFailedError, UnsupportedEmbeddedOperationError
from rclone_kit.job import _JobMonitor
from rclone_kit.operation import JobState, JobStatus, TransferStats
from rclone_kit.operations.transfer_ops_embedded import (
    cleanup_embedded,
    copy_bytes_embedded,
    copy_file_to_embedded,
    copy_files_embedded,
    delete_files_embedded,
    purge_dir_embedded,
)
from rclone_kit.rc.jobs import RcJobRef
from rclone_kit.types import SizeSuffix

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
    single-call, no-progress operations.

    `self.success`/`self.error` set the default outcome for every job;
    `queue_outcomes()` overrides that default for jobs started next, one
    outcome consumed per `start()` call in order - so a composite-operation
    test can make partition N fail while every other partition succeeds,
    without needing to know a job's ID in advance.
    """

    def __init__(self) -> None:
        self.starts: list[tuple[str, dict, str]] = []
        self._next_job_id = 1
        self.success = True
        self.error = ""
        self._queued_outcomes: list[tuple[bool, str]] = []
        self._outcome_by_job_id: dict[int, tuple[bool, str]] = {}
        self.files_from_contents: list[list[str]] = []

    def queue_outcomes(self, outcomes: list[tuple[bool, str]]) -> None:
        self._queued_outcomes = list(outcomes)

    def start(self, method: str, params: Mapping[str, object], group: str) -> RcJobRef:
        job_id = self._next_job_id
        self._next_job_id += 1
        self.starts.append((method, dict(params), group))
        filter_opt = params.get("_filter")
        if isinstance(filter_opt, dict) and filter_opt.get("FilesFrom"):
            # Read eagerly: the temp file is gone by the time the test
            # inspects this, since the caller's `TemporaryDirectory` has
            # already closed by then.
            (files_from_path,) = filter_opt["FilesFrom"]
            content = Path(files_from_path).read_text(encoding="utf-8")
            self.files_from_contents.append(content.splitlines())
        if self._queued_outcomes:
            self._outcome_by_job_id[job_id] = self._queued_outcomes.pop(0)
        return RcJobRef(job_id=job_id, execute_id=f"exec-{job_id}", group=group)

    def status(self, ref: RcJobRef) -> JobStatus:
        success, error = self._outcome_by_job_id.get(ref.job_id, (self.success, self.error))
        return JobStatus(
            job_id=ref.job_id,
            execute_id=ref.execute_id,
            group=ref.group,
            state=JobState.SUCCEEDED if success else JobState.FAILED,
            started_at=_NOW,
            ended_at=_NOW + timedelta(seconds=1),
            duration=1.0,
            error=None if success else (error or "boom"),
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


class TestCopyFilesEmbedded:
    def test_empty_file_list_starts_no_jobs_and_is_ok(self) -> None:
        job_client = FakeJobClient()

        result = copy_files_embedded(
            _monitor(job_client), _CLIENT_ID, _empty_config(), "src:", "dst:", []
        )

        assert result.ok is True
        assert result.job_ids == ()
        assert job_client.starts == []

    def test_rejects_other_args_without_starting_any_job(self) -> None:
        job_client = FakeJobClient()

        with pytest.raises(UnsupportedEmbeddedOperationError):
            copy_files_embedded(
                _monitor(job_client),
                _CLIENT_ID,
                _empty_config(),
                "src:",
                "dst:",
                ["a.txt"],
                other_args=["--foo"],
            )

        assert job_client.starts == []

    def test_rejects_a_remote_qualified_entry_without_starting_any_job(self) -> None:
        job_client = FakeJobClient()

        with pytest.raises(ValueError, match="not allowed for copy_files"):
            copy_files_embedded(
                _monitor(job_client), _CLIENT_ID, _empty_config(), "src:", "dst:", ["remote:a.txt"]
            )

        assert job_client.starts == []

    def test_single_partition_starts_one_rclonekit_copy_job(self) -> None:
        job_client = FakeJobClient()

        result = copy_files_embedded(
            _monitor(job_client),
            _CLIENT_ID,
            _empty_config(),
            "src:base",
            "dst:base",
            ["a.txt", "b.txt"],
        )

        assert result.ok is True
        assert len(job_client.starts) == 1
        method, params, _group = job_client.starts[0]
        assert method == "rclonekit/copy"
        assert params["srcFs"] == "src:base"
        assert params["dstFs"] == "dst:base"
        assert params["createEmptySrcDirs"] is False
        assert job_client.files_from_contents == [["a.txt", "b.txt"]]

    def test_default_transfer_tuning_matches_copy_historical_profile(self) -> None:
        job_client = FakeJobClient()

        copy_files_embedded(
            _monitor(job_client), _CLIENT_ID, _empty_config(), "src:base", "dst:base", ["a.txt"]
        )

        _method, params, _group = job_client.starts[0]
        assert params["_config"] == {
            "Checkers": 1000,
            "Transfers": 32,
            "LowLevelRetries": 10,
            "Retries": 3,
        }

    def test_overridden_transfer_tuning_is_encoded(self) -> None:
        job_client = FakeJobClient()

        copy_files_embedded(
            _monitor(job_client),
            _CLIENT_ID,
            _empty_config(),
            "src:base",
            "dst:base",
            ["a.txt"],
            checkers=4,
            transfers=2,
            low_level_retries=1,
            retries=1,
            retries_sleep="10s",
            timeout="5m",
            max_backlog=100,
            metadata=True,
        )

        _method, params, _group = job_client.starts[0]
        assert params["_config"] == {
            "Checkers": 4,
            "Transfers": 2,
            "LowLevelRetries": 1,
            "Retries": 1,
            "RetriesInterval": "10s",
            "Timeout": "5m",
            "MaxBacklog": 100,
            "Metadata": True,
        }

    def test_two_partitions_each_start_their_own_job(self) -> None:
        job_client = FakeJobClient()

        result = copy_files_embedded(
            _monitor(job_client),
            _CLIENT_ID,
            _empty_config(),
            "src:base",
            "dst:base",
            ["dirA/a.txt", "dirB/b.txt"],
        )

        assert result.ok is True
        assert len(job_client.starts) == 2
        assert len(result.job_ids) == 2
        src_values = {params["srcFs"] for _method, params, _group in job_client.starts}
        assert src_values == {"src:base/dirA", "src:base/dirB"}

    def test_one_failed_partition_does_not_abort_collecting_the_other(self) -> None:
        job_client = FakeJobClient()
        job_client.queue_outcomes([(False, "boom"), (True, "")])

        result = copy_files_embedded(
            _monitor(job_client),
            _CLIENT_ID,
            _empty_config(),
            "src:base",
            "dst:base",
            ["dirA/a.txt", "dirB/b.txt"],
            check=False,
        )

        assert len(job_client.starts) == 2
        assert result.ok is False
        assert len(result.job_ids) == 2
        assert len(result.warnings) == 1
        assert "boom" in (result.error or "")

    def test_check_true_raises_operation_failed_error_after_every_partition_settles(self) -> None:
        job_client = FakeJobClient()
        job_client.queue_outcomes([(False, "boom"), (True, "")])

        with pytest.raises(OperationFailedError) as excinfo:
            copy_files_embedded(
                _monitor(job_client),
                _CLIENT_ID,
                _empty_config(),
                "src:base",
                "dst:base",
                ["dirA/a.txt", "dirB/b.txt"],
                check=True,
            )

        assert len(job_client.starts) == 2
        assert len(excinfo.value.result.job_ids) == 2


class TestDeleteFilesEmbedded:
    def test_empty_file_list_starts_no_jobs_and_is_ok(self) -> None:
        job_client = FakeJobClient()

        result = delete_files_embedded(_monitor(job_client), _CLIENT_ID, _empty_config(), [])

        assert result.ok is True
        assert job_client.starts == []

    def test_rejects_other_args_without_starting_any_job(self) -> None:
        job_client = FakeJobClient()

        with pytest.raises(UnsupportedEmbeddedOperationError):
            delete_files_embedded(
                _monitor(job_client),
                _CLIENT_ID,
                _empty_config(),
                ["remote:a.txt"],
                other_args=["--foo"],
            )

        assert job_client.starts == []

    def test_single_partition_starts_an_operations_delete_job(self) -> None:
        job_client = FakeJobClient()

        result = delete_files_embedded(
            _monitor(job_client), _CLIENT_ID, _empty_config(), ["remote:bucket/a.txt"]
        )

        assert result.ok is True
        assert len(job_client.starts) == 1
        method, params, _group = job_client.starts[0]
        assert method == "operations/delete"
        assert params["fs"] == "remote:bucket"
        assert params["_config"] == {"Checkers": 1000, "Transfers": 1000}
        assert job_client.files_from_contents == [["a.txt"]]

    def test_rmdirs_true_follows_a_successful_delete_with_rmdirs(self) -> None:
        job_client = FakeJobClient()

        delete_files_embedded(
            _monitor(job_client), _CLIENT_ID, _empty_config(), ["remote:bucket/a.txt"], rmdirs=True
        )

        assert [method for method, _params, _group in job_client.starts] == [
            "operations/delete",
            "operations/rmdirs",
        ]
        _method, rmdirs_params, _group = job_client.starts[1]
        assert rmdirs_params == {"fs": "remote:bucket", "remote": "", "leaveRoot": True}

    def test_rmdirs_true_skips_rmdirs_when_delete_fails(self) -> None:
        job_client = FakeJobClient()
        job_client.success = False

        result = delete_files_embedded(
            _monitor(job_client),
            _CLIENT_ID,
            _empty_config(),
            ["remote:bucket/a.txt"],
            rmdirs=True,
            check=False,
        )

        assert [method for method, _params, _group in job_client.starts] == ["operations/delete"]
        assert result.ok is False

    def test_two_partitions_each_start_their_own_job(self) -> None:
        job_client = FakeJobClient()

        result = delete_files_embedded(
            _monitor(job_client),
            _CLIENT_ID,
            _empty_config(),
            ["remote1:bucket/a.txt", "remote2:bucket/b.txt"],
        )

        assert result.ok is True
        assert len(job_client.starts) == 2
        assert len(result.job_ids) == 2

    def test_check_true_raises_operation_failed_error_after_every_partition_settles(self) -> None:
        job_client = FakeJobClient()
        job_client.queue_outcomes([(False, "boom"), (True, "")])

        with pytest.raises(OperationFailedError) as excinfo:
            delete_files_embedded(
                _monitor(job_client),
                _CLIENT_ID,
                _empty_config(),
                ["remote1:bucket/a.txt", "remote2:bucket/b.txt"],
                check=True,
            )

        assert len(job_client.starts) == 2
        assert len(excinfo.value.result.job_ids) == 2


class TestCopyBytesEmbedded:
    def test_starts_readrange_job_with_split_fs_remote(self) -> None:
        job_client = FakeJobClient()

        copy_bytes_embedded(
            _monitor(job_client), _CLIENT_ID, "remote:path/to/a.txt", 10, 20, Path("out.bin")
        )

        assert len(job_client.starts) == 1
        method, params, _group = job_client.starts[0]
        assert method == "rclonekit/readrange"
        assert params["fs"] == "remote:path/to"
        assert params["remote"] == "a.txt"
        assert params["offset"] == 10
        assert params["count"] == 20
        assert params["outputPath"] == str(Path("out.bin"))

    def test_accepts_size_suffix_offset_and_length(self) -> None:
        job_client = FakeJobClient()

        copy_bytes_embedded(
            _monitor(job_client),
            _CLIENT_ID,
            "remote:a.txt",
            SizeSuffix("1K"),
            SizeSuffix("2K"),
            Path("out.bin"),
        )

        _method, params, _group = job_client.starts[0]
        assert params["offset"] == 1024
        assert params["count"] == 2048

    def test_returns_ok_result_on_success(self) -> None:
        job_client = FakeJobClient()

        result = copy_bytes_embedded(
            _monitor(job_client), _CLIENT_ID, "remote:a.txt", 0, 10, Path("out.bin")
        )

        assert result.ok is True

    def test_raises_operation_failed_error_on_failure(self) -> None:
        job_client = FakeJobClient()
        job_client.success = False

        with pytest.raises(OperationFailedError):
            copy_bytes_embedded(
                _monitor(job_client), _CLIENT_ID, "remote:a.txt", 0, 10, Path("out.bin")
            )
