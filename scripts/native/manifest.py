"""Generated-manifest model for one completed native build.

`scripts/native/build.py` is the only writer. Kept separate so the manifest
shape (and the source/toolchain provenance it records) can be unit tested
with fake subprocess/git results, without invoking a real Go toolchain.
"""

import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rclone_kit.runtime.hashing import sha256_of_file
from rclone_kit.runtime.native_platform import NativeTarget

_MANIFEST_SCHEMA_VERSION = 1


class NativeManifestError(Exception):
    """Raised when a required piece of build provenance cannot be
    determined, so a manifest is never written with silently wrong data.
    """


@dataclass(frozen=True)
class ForkProvenance:
    """Identifies the exact `native/rclone` submodule commit a build was
    produced from.
    """

    url: str
    commit: str
    branch: str
    worktree_clean: bool


@dataclass(frozen=True)
class ToolchainProvenance:
    """Identifies the exact Go/C toolchain a build was produced with."""

    go_version: str
    goos: str
    goarch: str
    cgo_enabled: str
    c_compiler_path: str
    c_compiler_identity: str


@dataclass(frozen=True)
class OutputFile:
    """One file staged into the build output directory, with its digest and
    size recorded so `SHA256SUMS` and the manifest can never disagree.
    """

    filename: str
    sha256_digest: str
    size_bytes: int


@dataclass(frozen=True)
class NativeBuildManifest:
    """The complete generated manifest for one native build, written as
    `native-manifest.json` alongside its outputs.
    """

    schema: int
    rclone_kit_version: str
    c_abi_version: int
    rclone_upstream_version: str
    fork: ForkProvenance
    toolchain: ToolchainProvenance
    target_os: str
    target_arch: str
    wheel_platform_tag: str
    go_build_tags: tuple[str, ...]
    outputs: tuple[OutputFile, ...] = field(default_factory=tuple)


def _run(command: list[str], cwd: Path) -> str:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        joined = " ".join(command)
        raise NativeManifestError(
            f"Command failed ({result.returncode}): {joined}\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def fork_provenance(submodule_dir: Path, fork_url: str) -> ForkProvenance:
    """Inspect the `native/rclone` submodule's current checkout."""
    commit = _run(["git", "rev-parse", "HEAD"], submodule_dir)
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], submodule_dir)
    status = _run(["git", "status", "--porcelain"], submodule_dir)
    return ForkProvenance(url=fork_url, commit=commit, branch=branch, worktree_clean=status == "")


def toolchain_provenance(go_executable: str, cc_path: str) -> ToolchainProvenance:
    """Inspect the resolved Go and C toolchain used for a build."""
    go_version = _run([go_executable, "version"], Path.cwd())
    goos = _run([go_executable, "env", "GOOS"], Path.cwd())
    goarch = _run([go_executable, "env", "GOARCH"], Path.cwd())
    cgo_enabled = _run([go_executable, "env", "CGO_ENABLED"], Path.cwd())
    cc_identity_lines = _run([cc_path, "--version"], Path.cwd()).splitlines()
    cc_identity = cc_identity_lines[0] if cc_identity_lines else ""
    return ToolchainProvenance(
        go_version=go_version,
        goos=goos,
        goarch=goarch,
        cgo_enabled=cgo_enabled,
        c_compiler_path=cc_path,
        c_compiler_identity=cc_identity,
    )


def output_files(output_dir: Path, filenames: list[str]) -> tuple[OutputFile, ...]:
    """Hash and size every named output file, in the given order."""
    return tuple(
        OutputFile(
            filename=filename,
            sha256_digest=sha256_of_file(output_dir / filename),
            size_bytes=(output_dir / filename).stat().st_size,
        )
        for filename in filenames
    )


def build_manifest(
    *,
    rclone_kit_version: str,
    c_abi_version: int,
    rclone_upstream_version: str,
    fork: ForkProvenance,
    toolchain: ToolchainProvenance,
    target: NativeTarget,
    outputs: tuple[OutputFile, ...],
    go_build_tags: tuple[str, ...] | None = None,
) -> NativeBuildManifest:
    """Build the manifest for one completed native build.

    `go_build_tags` records the tags this *specific build* actually used
    (e.g. `("cmount",)` for a `--profile production` build) - it is not
    read from `target.go_build_tags`, which only describes the platform,
    not which profile produced this particular set of outputs. Defaults to
    `target.go_build_tags` (empty) when not given, for callers that only
    ever build one profile.
    """
    return NativeBuildManifest(
        schema=_MANIFEST_SCHEMA_VERSION,
        rclone_kit_version=rclone_kit_version,
        c_abi_version=c_abi_version,
        rclone_upstream_version=rclone_upstream_version,
        fork=fork,
        toolchain=toolchain,
        target_os=target.operating_system.value,
        target_arch=target.architecture.value,
        wheel_platform_tag=target.wheel_platform_tag,
        go_build_tags=go_build_tags if go_build_tags is not None else target.go_build_tags,
        outputs=outputs,
    )


def write_manifest(manifest: NativeBuildManifest, destination: Path) -> None:
    """Serialize `manifest` as pretty-printed JSON to `destination`."""
    destination.write_text(json.dumps(asdict(manifest), indent=2) + "\n", encoding="utf-8")


def write_sha256sums(outputs: tuple[OutputFile, ...], destination: Path) -> None:
    """Write a `sha256sum`-compatible `SHA256SUMS` file for `outputs`."""
    lines = [f"{output.sha256_digest}  {output.filename}" for output in outputs]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
