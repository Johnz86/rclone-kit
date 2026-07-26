"""Native-backed parity check for the embedded composite transfer
operations `copy_files`/`delete_files`.

`copy_files()` never needs a registered remote - its file entries are
plain relative names under a caller-supplied local `src`/`dst`.
`delete_files()`'s entries must be fully qualified `remote:path` references
(enforced by `group_files.parse_file`), so these tests register a fresh,
uniquely-named `type = local` remote per test via `config/create`
(confirmed empirically to resolve relative paths against the current
working directory) rather than reaching for a real cloud remote.

Skipped automatically when no built native library exists (run
`scripts/native/build.py` first).
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import NATIVE_LIBRARY_AVAILABLE

from rclone_kit.client import Rclone
from rclone_kit.exceptions import OperationFailedError
from rclone_kit.native.runtime import RcloneRuntime
from rclone_kit.rc.client import RcClient

pytestmark = pytest.mark.skipif(
    not NATIVE_LIBRARY_AVAILABLE,
    reason="No built native library found; run scripts/native/build.py first.",
)


@pytest.fixture
def local_remote(native_runtime: RcloneRuntime) -> Iterator[str]:
    """Register a fresh `type = local` remote against the shared session
    runtime's config and return its bare name (without the trailing `:`),
    removing it again afterward - `native_runtime`'s config is session-
    scoped and shared with every other native test module, so a remote
    left behind here would otherwise leak into unrelated tests (e.g.
    `test_listing_ops_embedded_integration.py`'s "no remotes configured"
    assertion). A unique name per call also means no test can ever observe
    another test's stale `Fs` cache entry for the same fs string (see
    `RcPath`/`_resolve_local`'s docstring for why that matters for a
    shared, long-lived embedded runtime).
    """
    name = f"testlocal-{uuid.uuid4().hex}"
    rc_client = RcClient(native_runtime)
    rc_client.call("config/create", name=name, type="local", parameters={})
    yield name
    rc_client.call("config/delete", name=name)


def test_copy_files_for_a_selected_subset(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    embedded_dst = tmp_path / "embedded_dst"
    src.mkdir()
    embedded_dst.mkdir()
    (src / "a.txt").write_bytes(b"aaa")
    (src / "b.txt").write_bytes(b"bbb")
    (src / "c.txt").write_bytes(b"ignored")

    embedded.copy_files(str(src), str(embedded_dst), ["a.txt", "b.txt"])

    assert (embedded_dst / "a.txt").read_bytes() == b"aaa"
    assert (embedded_dst / "b.txt").read_bytes() == b"bbb"
    assert not (embedded_dst / "c.txt").exists()


def test_copy_files_two_partitions_both_copy(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "dirA").mkdir(parents=True)
    (src / "dirB").mkdir(parents=True)
    dst.mkdir()
    (src / "dirA" / "a.txt").write_bytes(b"A")
    (src / "dirB" / "b.txt").write_bytes(b"B")

    result = embedded.copy_files(str(src), str(dst), ["dirA/a.txt", "dirB/b.txt"])

    assert result.ok is True
    assert (dst / "dirA" / "a.txt").read_bytes() == b"A"
    assert (dst / "dirB" / "b.txt").read_bytes() == b"B"


def test_copy_files_missing_source_file_is_silently_skipped(
    tmp_path: Path, embedded: Rclone
) -> None:
    # Empirically confirmed (not assumed): a `FilesFrom` entry naming a
    # nonexistent source file is simply not visited during the march/walk,
    # the same non-raising contract already confirmed for
    # `operations/delete` (T08) - it is not an error for either.
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "real.txt").write_bytes(b"real")

    result = embedded.copy_files(str(src), str(dst), ["missing.txt", "real.txt"])

    assert result.ok is True
    assert (dst / "real.txt").read_bytes() == b"real"
    assert not (dst / "missing.txt").exists()


def test_copy_files_one_failed_partition_does_not_abort_the_other(
    tmp_path: Path, embedded: Rclone
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "dirA").mkdir(parents=True)
    (src / "dirB").mkdir(parents=True)
    dst.mkdir()
    (src / "dirA" / "a.txt").write_bytes(b"A")
    (src / "dirB" / "b.txt").write_bytes(b"B")
    # dirA's destination copy will fail: "a.txt" already exists there as a
    # directory, so rclone's rename-into-place step cannot complete.
    (dst / "dirA").mkdir()
    (dst / "dirA" / "a.txt").mkdir()

    result = embedded.copy_files(str(src), str(dst), ["dirA/a.txt", "dirB/b.txt"], check=False)

    assert result.ok is False
    assert (dst / "dirB" / "b.txt").read_bytes() == b"B"


def test_copy_files_check_true_raises_operation_failed_error(
    tmp_path: Path, embedded: Rclone
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_bytes(b"A")
    (dst / "a.txt").mkdir()

    with pytest.raises(OperationFailedError):
        embedded.copy_files(str(src), str(dst), ["a.txt"], check=True)


def test_copy_files_empty_list_is_a_noop(tmp_path: Path, embedded: Rclone) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()

    result = embedded.copy_files(str(src), str(dst), [])

    assert result.ok is True


def test_delete_files_removes_only_the_listed_files(
    tmp_path: Path, embedded: Rclone, local_remote: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `group_files()` keys a partition by every file's *immediate parent*
    # directory - a bare top-level "remote:a.txt" reference
    # (no subdirectory) hits a separate, pre-existing `group_files` quirk
    # (the grouping key loses its trailing `:`), so every entry here lives
    # under a "sub" subdirectory to stay on the well-supported path.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_bytes(b"a")
    (tmp_path / "sub" / "b.txt").write_bytes(b"b")

    result = embedded.delete_files([f"{local_remote}:sub/a.txt"])

    assert result.ok is True
    assert not (tmp_path / "sub" / "a.txt").exists()
    assert (tmp_path / "sub" / "b.txt").exists()


def test_delete_files_missing_file_does_not_raise(
    tmp_path: Path, embedded: Rclone, local_remote: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()

    result = embedded.delete_files([f"{local_remote}:sub/does-not-exist.txt"])

    assert result.ok is True


def test_delete_files_rmdirs_true_cleans_up_without_removing_the_group_root(
    tmp_path: Path, embedded: Rclone, local_remote: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `rmdirs=True` reproduces `delete`'s own `--rmdirs` sequence: a second
    # `operations/rmdirs(leaveRoot=True)` call rooted at the SAME group key
    # `operations/delete` used (here, "sub" - the deleted file's immediate
    # parent). It cleans up any other now-or-already-empty subdirectory
    # under that root, but - because of `leaveRoot` - never the root
    # itself, even though the root is also the deleted file's own
    # directory and ends up containing nothing else.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "only.txt").write_bytes(b"x")
    (tmp_path / "sub" / "already_empty").mkdir()

    result = embedded.delete_files([f"{local_remote}:sub/only.txt"], rmdirs=True)

    assert result.ok is True
    assert (tmp_path / "sub").exists()
    assert not (tmp_path / "sub" / "only.txt").exists()
    assert not (tmp_path / "sub" / "already_empty").exists()


def test_delete_files_empty_list_is_a_noop(embedded: Rclone) -> None:
    result = embedded.delete_files([])

    assert result.ok is True
