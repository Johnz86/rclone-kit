"""Unit tests for `rclone_kit.util.rclone_execute`'s subprocess lifecycle.

These tests replace `subprocess.Popen` with a fake so no real process is
spawned; they exist to prove that `rclone_execute` tracks its subprocess in
`util._LIVE_SUBPROCESSES` for the duration of the call and always discards it
afterward, without registering a per-call `atexit` callback (the pattern
that used to leak one closure per invocation for the life of the process).
"""

from pathlib import Path

import pytest

from rclone_kit import util


class _FakePopen:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.pid = 4242
        self.returncode = 0
        self.seen_live_during_communicate = False

    def communicate(self) -> tuple[str, str]:
        self.seen_live_during_communicate = self in util._LIVE_SUBPROCESSES
        return "out", ""


class _FakeFailingPopen(_FakePopen):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.returncode = 1


def test_rclone_execute_tracks_and_discards_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: list[_FakePopen] = []

    def fake_popen(*args: object, **kwargs: object) -> _FakePopen:
        proc = _FakePopen(*args, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(util.subprocess, "Popen", fake_popen)

    util.rclone_execute(
        cmd=["version"], rclone_conf=None, rclone_exe=tmp_path / "rclone", check=False
    )

    assert created[0].seen_live_during_communicate is True
    assert created[0] not in util._LIVE_SUBPROCESSES


def test_rclone_execute_discards_subprocess_on_check_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: list[_FakeFailingPopen] = []

    def fake_popen(*args: object, **kwargs: object) -> _FakeFailingPopen:
        proc = _FakeFailingPopen(*args, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(util.subprocess, "Popen", fake_popen)

    with pytest.raises(util.subprocess.CalledProcessError):
        util.rclone_execute(
            cmd=["version"], rclone_conf=None, rclone_exe=tmp_path / "rclone", check=True
        )

    assert created[0] not in util._LIVE_SUBPROCESSES


def test_rclone_execute_does_not_register_atexit_per_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    register_calls: list[object] = []
    monkeypatch.setattr(util.subprocess, "Popen", lambda *_args, **_kwargs: _FakePopen())
    monkeypatch.setattr(util.atexit, "register", register_calls.append)

    util.rclone_execute(
        cmd=["version"], rclone_conf=None, rclone_exe=tmp_path / "rclone", check=False
    )
    util.rclone_execute(
        cmd=["version"], rclone_conf=None, rclone_exe=tmp_path / "rclone", check=False
    )

    assert register_calls == []
