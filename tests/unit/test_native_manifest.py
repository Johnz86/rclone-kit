"""Unit tests for `scripts/native/manifest.py`.

Uses `monkeypatch` on `subprocess.run` so these tests exercise the manifest
shape and parsing logic without invoking a real `git`/`go`/C-compiler
toolchain; the actual end-to-end build is proven separately by running
`scripts/native/build.py` against the real toolchain.
"""

import json
import subprocess
from pathlib import Path

import manifest as native_manifest
import pytest

from rclone_kit.runtime.native_platform import WINDOWS_AMD64_NATIVE_TARGET


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(returncode=1, stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(native_manifest.NativeManifestError, match="boom"):
        native_manifest._run(["git", "status"], tmp_path)


def test_fork_provenance_reports_clean_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    responses = {
        "rev-parse HEAD": "abc123",
        "rev-parse --abbrev-ref HEAD": "main",
        "status --porcelain": "",
    }

    def fake_run(command: list[str], **_kwargs: object) -> _FakeCompletedProcess:
        key = " ".join(command[1:])
        return _FakeCompletedProcess(returncode=0, stdout=responses[key])

    monkeypatch.setattr(subprocess, "run", fake_run)
    fork = native_manifest.fork_provenance(tmp_path, "https://example.com/fork.git")
    assert fork.commit == "abc123"
    assert fork.branch == "main"
    assert fork.worktree_clean is True
    assert fork.url == "https://example.com/fork.git"


def test_fork_provenance_reports_dirty_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    responses = {
        "rev-parse HEAD": "abc123",
        "rev-parse --abbrev-ref HEAD": "main",
        "status --porcelain": " M some_file.go",
    }

    def fake_run(command: list[str], **_kwargs: object) -> _FakeCompletedProcess:
        key = " ".join(command[1:])
        return _FakeCompletedProcess(returncode=0, stdout=responses[key])

    monkeypatch.setattr(subprocess, "run", fake_run)
    fork = native_manifest.fork_provenance(tmp_path, "https://example.com/fork.git")
    assert fork.worktree_clean is False


def test_output_files_hashes_and_sizes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "b.txt").write_bytes(b"world!")

    outputs = native_manifest.output_files(tmp_path, ["a.txt", "b.txt"])

    assert [output.filename for output in outputs] == ["a.txt", "b.txt"]
    assert outputs[0].size_bytes == 5
    assert outputs[1].size_bytes == 6
    assert len(outputs[0].sha256_digest) == 64
    assert outputs[0].sha256_digest != outputs[1].sha256_digest


def test_write_manifest_round_trips_as_json(tmp_path: Path) -> None:
    fork = native_manifest.ForkProvenance(
        url="https://example.com/fork.git", commit="abc123", branch="main", worktree_clean=True
    )
    toolchain = native_manifest.ToolchainProvenance(
        go_version="go version go1.26.5 windows/amd64",
        goos="windows",
        goarch="amd64",
        cgo_enabled="1",
        c_compiler_path="C:\\cc.exe",
        c_compiler_identity="clang version 22.1.8",
    )
    outputs = (
        native_manifest.OutputFile(filename="rclone.exe", sha256_digest="a" * 64, size_bytes=123),
    )
    built = native_manifest.build_manifest(
        rclone_kit_version="1.0.0",
        c_abi_version=1,
        rclone_upstream_version="1.74.x",
        fork=fork,
        toolchain=toolchain,
        target=WINDOWS_AMD64_NATIVE_TARGET,
        outputs=outputs,
    )

    destination = tmp_path / "native-manifest.json"
    native_manifest.write_manifest(built, destination)

    decoded = json.loads(destination.read_text(encoding="utf-8"))
    assert decoded["schema"] == 1
    assert decoded["rclone_kit_version"] == "1.0.0"
    assert decoded["fork"]["commit"] == "abc123"
    assert decoded["toolchain"]["goos"] == "windows"
    assert decoded["wheel_platform_tag"] == WINDOWS_AMD64_NATIVE_TARGET.wheel_platform_tag
    assert decoded["outputs"][0]["filename"] == "rclone.exe"


def test_write_sha256sums_matches_sha256sum_format(tmp_path: Path) -> None:
    outputs = (
        native_manifest.OutputFile(filename="rclone.exe", sha256_digest="a" * 64, size_bytes=1),
        native_manifest.OutputFile(
            filename="librclone_kit.dll", sha256_digest="b" * 64, size_bytes=2
        ),
    )
    destination = tmp_path / "SHA256SUMS"
    native_manifest.write_sha256sums(outputs, destination)

    lines = destination.read_text(encoding="utf-8").splitlines()
    assert lines == [f"{'a' * 64}  rclone.exe", f"{'b' * 64}  librclone_kit.dll"]
