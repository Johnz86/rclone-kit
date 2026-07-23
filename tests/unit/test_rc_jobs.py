"""Unit tests for `rclone_kit.rc.jobs`' RC job boundary
(`RcloneRcJobClient`): request mapping, strict response parsing, and the
"job not found" / identity-mismatch error paths.

Uses a fake `RcCallable` driven by canned per-method responses (or
exceptions), so these tests exercise request/response mapping without a
built native library. Native-DLL shape verification (that these parsers
actually match real rclone JSON) is a separate concern; the shapes
asserted here were captured from ad hoc probes against the real built
library during development (see the module docstring in `rc/jobs.py`).
"""

import pytest

from rclone_kit.exceptions import JobIdentityError, OperationStartError
from rclone_kit.operation import JobState
from rclone_kit.rc.errors import RcCallError
from rclone_kit.rc.jobs import RcJobNotFoundError, RcJobRef, RcloneRcJobClient


class FakeRcClient:
    """A fake `RcCallable` returning one queued response (or raising one
    queued exception) per call to a given method, repeating the last
    queued entry once exhausted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._queues: dict[str, list[object]] = {}

    def queue(self, method: str, *responses: object) -> None:
        self._queues[method] = list(responses)

    def call(self, method: str, **params: object) -> dict:
        self.calls.append((method, params))
        queue = self._queues.get(method)
        if not queue:
            raise AssertionError(f"no queued response for {method!r}")
        response = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, dict)
        return response


_JOB_NOT_FOUND_ERROR = RcCallError(
    "job/status", 500, {"error": "job not found", "input": {"jobid": 1}}
)


def _running_status(*, job_id: int = 1, execute_id: str = "exec-1", group: str = "g") -> dict:
    return {
        "duration": 0,
        "endTime": "0001-01-01T00:00:00Z",
        "error": "",
        "executeId": execute_id,
        "finished": False,
        "group": group,
        "id": job_id,
        "output": None,
        "startTime": "2026-07-23T20:24:09.8370177+02:00",
        "success": False,
    }


def _finished_status(*, ok: bool, error: str = "", **overrides: object) -> dict:
    base = {
        "duration": 0.0419614,
        "endTime": "2026-07-23T20:24:38.3862217+02:00",
        "error": error,
        "executeId": "exec-1",
        "finished": True,
        "group": "g",
        "id": 1,
        "output": {},
        "startTime": "2026-07-23T20:24:38.3442603+02:00",
        "success": ok,
    }
    base.update(overrides)
    return base


def _stats_payload(**overrides: object) -> dict:
    base: dict[str, object] = {
        "bytes": 3000,
        "checks": 0,
        "elapsedTime": 0.0931607,
        "errors": 0,
        "eta": None,
        "fatalError": False,
        "retryError": False,
        "speed": 0,
        "totalBytes": 3000,
        "totalChecks": 0,
        "totalTransfers": 2,
        "transfers": 2,
    }
    base.update(overrides)
    return base


class TestStart:
    def test_start_sends_async_and_group(self) -> None:
        client = FakeRcClient()
        client.queue("sync/copy", {"executeId": "exec-1", "jobid": 7})
        job_client = RcloneRcJobClient(client)

        ref = job_client.start("sync/copy", {"srcFs": "a", "dstFs": "b"}, group="g1")

        assert ref == RcJobRef(job_id=7, execute_id="exec-1", group="g1")
        assert client.calls == [
            ("sync/copy", {"srcFs": "a", "dstFs": "b", "_async": True, "_group": "g1"})
        ]

    def test_start_failure_raises_operation_start_error(self) -> None:
        client = FakeRcClient()
        cause = RcCallError("sync/copy", 500, {"error": "directory not found"})
        client.queue("sync/copy", cause)
        job_client = RcloneRcJobClient(client)

        with pytest.raises(OperationStartError) as excinfo:
            job_client.start("sync/copy", {}, group="g1")
        assert excinfo.value.__cause__ is cause

    def test_start_missing_jobid_raises_value_error(self) -> None:
        client = FakeRcClient()
        client.queue("sync/copy", {"executeId": "exec-1"})
        job_client = RcloneRcJobClient(client)

        with pytest.raises(KeyError):
            job_client.start("sync/copy", {}, group="g1")

    def test_start_boolean_jobid_is_rejected(self) -> None:
        client = FakeRcClient()
        client.queue("sync/copy", {"executeId": "exec-1", "jobid": True})
        job_client = RcloneRcJobClient(client)

        with pytest.raises(ValueError, match="jobid"):
            job_client.start("sync/copy", {}, group="g1")


class TestStatus:
    def test_running_status_parses_to_running_state_with_no_ended_at(self) -> None:
        client = FakeRcClient()
        client.queue("job/status", _running_status())
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        status = job_client.status(ref)

        assert status.state is JobState.RUNNING
        assert status.ended_at is None
        assert status.error is None

    def test_finished_success_parses_to_succeeded(self) -> None:
        client = FakeRcClient()
        client.queue("job/status", _finished_status(ok=True))
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        status = job_client.status(ref)

        assert status.state is JobState.SUCCEEDED
        assert status.ended_at is not None
        assert status.error is None

    def test_finished_failure_parses_to_failed_with_error(self) -> None:
        client = FakeRcClient()
        client.queue("job/status", _finished_status(ok=False, error="directory not found"))
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        status = job_client.status(ref)

        assert status.state is JobState.FAILED
        assert status.error == "directory not found"

    def test_empty_error_string_becomes_none(self) -> None:
        client = FakeRcClient()
        client.queue("job/status", _finished_status(ok=True, error=""))
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        assert job_client.status(ref).error is None

    def test_job_not_found_raises_rc_job_not_found_error(self) -> None:
        client = FakeRcClient()
        client.queue("job/status", _JOB_NOT_FOUND_ERROR)
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        with pytest.raises(RcJobNotFoundError) as excinfo:
            job_client.status(ref)
        assert excinfo.value.job_id == 1
        assert excinfo.value.__cause__ is _JOB_NOT_FOUND_ERROR

    def test_other_rc_failures_propagate_unwrapped(self) -> None:
        client = FakeRcClient()
        other_error = RcCallError("job/status", 500, {"error": "internal error"})
        client.queue("job/status", other_error)
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        with pytest.raises(RcCallError) as excinfo:
            job_client.status(ref)
        assert excinfo.value is other_error

    def test_execute_id_mismatch_raises_job_identity_error(self) -> None:
        client = FakeRcClient()
        client.queue("job/status", _finished_status(ok=True, executeId="exec-DIFFERENT"))
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        with pytest.raises(JobIdentityError) as excinfo:
            job_client.status(ref)
        assert excinfo.value.job_id == 1
        assert excinfo.value.expected_execute_id == "exec-1"
        assert excinfo.value.actual_execute_id == "exec-DIFFERENT"

    def test_missing_finished_field_raises_value_error(self) -> None:
        client = FakeRcClient()
        payload = _running_status()
        del payload["finished"]
        client.queue("job/status", payload)
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        with pytest.raises(KeyError):
            job_client.status(ref)

    def test_boolean_masquerading_as_finished_flag_is_rejected_if_wrong_type(self) -> None:
        client = FakeRcClient()
        payload = _running_status()
        payload["finished"] = "false"  # string, not bool
        client.queue("job/status", payload)
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        with pytest.raises(ValueError, match="finished"):
            job_client.status(ref)

    def test_zero_start_time_is_rejected(self) -> None:
        client = FakeRcClient()
        payload = _running_status()
        payload["startTime"] = "0001-01-01T00:00:00Z"
        client.queue("job/status", payload)
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        with pytest.raises(ValueError, match="zero time"):
            job_client.status(ref)

    def test_running_then_finished_polling_sequence(self) -> None:
        client = FakeRcClient()
        client.queue("job/status", _running_status(), _running_status(), _finished_status(ok=True))
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        states = [job_client.status(ref).state for _ in range(3)]

        assert states == [JobState.RUNNING, JobState.RUNNING, JobState.SUCCEEDED]


class TestStop:
    def test_stop_sends_jobid(self) -> None:
        client = FakeRcClient()
        client.queue("job/stop", {})
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        job_client.stop(ref)

        assert client.calls == [("job/stop", {"jobid": 1})]

    def test_stop_on_already_gone_job_is_idempotent(self) -> None:
        client = FakeRcClient()
        client.queue(
            "job/stop", RcCallError("job/stop", 500, {"error": "job not found", "input": {}})
        )
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        job_client.stop(ref)  # must not raise

    def test_stop_other_failures_propagate(self) -> None:
        client = FakeRcClient()
        other_error = RcCallError("job/stop", 500, {"error": "internal error"})
        client.queue("job/stop", other_error)
        job_client = RcloneRcJobClient(client)
        ref = RcJobRef(job_id=1, execute_id="exec-1", group="g")

        with pytest.raises(RcCallError):
            job_client.stop(ref)


class TestStats:
    def test_parses_totals(self) -> None:
        client = FakeRcClient()
        client.queue("core/stats", _stats_payload())
        job_client = RcloneRcJobClient(client)

        stats = job_client.stats("g")

        assert stats.bytes == 3000
        assert stats.total_transfers == 2
        assert client.calls == [("core/stats", {"group": "g"})]

    def test_missing_transferring_and_checking_become_empty_tuples(self) -> None:
        client = FakeRcClient()
        client.queue("core/stats", _stats_payload())
        job_client = RcloneRcJobClient(client)

        stats = job_client.stats("g")

        assert stats.active_transfers == ()
        assert stats.active_checks == ()

    def test_transferring_array_is_parsed(self) -> None:
        client = FakeRcClient()
        client.queue(
            "core/stats",
            _stats_payload(
                transferring=[
                    {
                        "name": "a.txt",
                        "bytes": 50,
                        "size": 100,
                        "speed": 10.0,
                        "percentage": 50,
                        "eta": 5.0,
                    }
                ]
            ),
        )
        job_client = RcloneRcJobClient(client)

        stats = job_client.stats("g")

        assert len(stats.active_transfers) == 1
        transfer = stats.active_transfers[0]
        assert transfer.name == "a.txt"
        assert transfer.bytes == 50
        assert transfer.eta_seconds == 5.0

    def test_checking_array_of_names_is_parsed(self) -> None:
        client = FakeRcClient()
        client.queue("core/stats", _stats_payload(checking=["a.txt", "b.txt"]))
        job_client = RcloneRcJobClient(client)

        stats = job_client.stats("g")

        assert stats.active_checks == ("a.txt", "b.txt")

    def test_null_eta_becomes_none(self) -> None:
        client = FakeRcClient()
        client.queue("core/stats", _stats_payload(eta=None))
        job_client = RcloneRcJobClient(client)

        assert job_client.stats("g").eta_seconds is None

    def test_bool_masquerading_as_bytes_is_rejected(self) -> None:
        client = FakeRcClient()
        client.queue("core/stats", _stats_payload(bytes=True))
        job_client = RcloneRcJobClient(client)

        with pytest.raises(ValueError, match="bytes"):
            job_client.stats("g")

    def test_non_list_transferring_is_rejected(self) -> None:
        client = FakeRcClient()
        client.queue("core/stats", _stats_payload(transferring={"not": "a list"}))
        job_client = RcloneRcJobClient(client)

        with pytest.raises(ValueError, match="transferring"):
            job_client.stats("g")


class TestDeleteStats:
    def test_delete_stats_sends_group(self) -> None:
        client = FakeRcClient()
        client.queue("core/stats-delete", {})
        job_client = RcloneRcJobClient(client)

        job_client.delete_stats("g")

        assert client.calls == [("core/stats-delete", {"group": "g"})]
