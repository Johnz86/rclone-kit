"""Proves that every currently-ported embedded operation completes without
spawning a subprocess.

This is the "no silent subprocess fallback" migration invariant made
executable: `subprocess.Popen` is patched to raise before a single
currently-ported public `Rclone` method is called against the real
built native library, so a regression that quietly reintroduces a CLI
call (directly or through a helper that still shells out) fails loudly
here instead of only showing up as a slow/flaky parity mismatch.

Deliberately uses the real DLL (not a fake `RcClient`) - a fake can't prove
the *production* code path takes no subprocess branch, only that the test's
own fake wasn't asked to. Skipped automatically when no built native
library exists (run `scripts/native/build.py` first).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from conftest import NATIVE_LIBRARY_AVAILABLE
from rclone_kit.client import Rclone

if TYPE_CHECKING:
    from rclone_kit.native.runtime import RcloneRuntime

pytestmark = pytest.mark.skipif(
    not NATIVE_LIBRARY_AVAILABLE,
    reason="No built native library found; run scripts/native/build.py first.",
)


class _SubprocessSpawnedError(AssertionError):
    def __init__(self, args: object) -> None:
        super().__init__(f"an embedded operation spawned a subprocess: {args!r}")


@pytest.fixture
def no_subprocess_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _RejectPopen(subprocess.Popen):
        def __init__(self, popen_args: object, **_kwargs: object) -> None:
            raise _SubprocessSpawnedError(popen_args)

    monkeypatch.setattr(subprocess, "Popen", _RejectPopen)


def test_every_currently_ported_embedded_method_avoids_subprocess(
    tmp_path: Path,
    native_runtime: RcloneRuntime,
    no_subprocess_allowed: None,
) -> None:
    rclone = Rclone(None, runtime=native_runtime)

    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    purge_target = tmp_path / "purge_target"
    src_dir.mkdir()
    dst_dir.mkdir()
    (src_dir / "sub").mkdir()
    (src_dir / "a.txt").write_bytes(b"hello")
    (src_dir / "sub" / "b.txt").write_bytes(b"world")
    purge_target.mkdir()
    (purge_target / "c.txt").write_bytes(b"purge me")

    # M01, M02, M03, M05 (M06 is covered separately by test_client_embedded.py:
    # it raises ValueError for a non-S3/unknown remote before touching the RC
    # boundary at all, which isn't useful to exercise here)
    rclone.obscure("secret")
    rclone.listremotes()
    rclone.config_paths()
    rclone.is_s3(str(src_dir))

    # L01, L05, L06, L07, L08, L10, L11, L12, L13, L14
    rclone.ls(str(src_dir), max_depth=-1)
    rclone.stat(str(src_dir / "a.txt"))
    rclone.modtime(str(src_dir / "a.txt"))
    rclone.modtime_dt(str(src_dir / "a.txt"))
    rclone.size_file(str(src_dir / "a.txt"))
    rclone.exists(str(src_dir / "a.txt"))
    rclone.is_synced(str(src_dir), str(src_dir))
    list(rclone.diff(str(src_dir), str(src_dir)))
    list(rclone.walk(str(src_dir)))
    list(rclone.scan_missing_folders(str(src_dir), str(dst_dir)))

    # T01, T02, T07, T11, T12
    copied = tmp_path / "copied.txt"
    rclone.copy_to(str(src_dir / "a.txt"), str(copied))
    rclone.read_bytes(str(src_dir / "a.txt"))
    rclone.read_text(str(src_dir / "a.txt"))
    rclone.purge(str(purge_target))
    rclone.cleanup(str(src_dir))
