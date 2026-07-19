# Implementation and build pipeline

## Purpose and status

This is the current maintainer guide for the `rclone-kit` implementation,
test strategy, build pipeline, and safe contribution workflow. It replaces
the historical build and source improvement proposals: many build
recommendations and focused source fixes were implemented, while the larger
source redesign remains future work.

The authoritative configuration remains in the code:

- `pyproject.toml` defines Python support, dependencies, entry points, build
  backend, and quality-tool configuration.
- `.python-version` pins the Python patch release used by maintainers and CI.
- `uv.lock` pins the resolved development environment.
- `src/rclone_kit/runtime/platform.py` defines the supported platforms, rclone
  version, download URLs, hashes, executable names, and wheel tags.
- `.github/workflows/ci.yml` defines the required CI graph.
- `docs/release_process.md` defines release recording and publication.

Update this guide when any of those contracts changes.

## Implementation overview

The main execution path is:

```text
Application / console script
        |
        v
Rclone public facade (rclone_kit/__init__.py)
        |
        v
RcloneImpl operation layer
        |
        v
RcloneExec + Process / rclone_execute
        |
        v
verified rclone executable + temporary rclone config
        |
        v
local or remote storage
```

### Public API and operations

`rclone_kit.Rclone` is the stable public facade. It exposes listing, copying,
deletion, remote-control, HTTP serving, mounting, filesystem, database, and
S3 operations. Most methods delegate to `RcloneImpl`, which currently owns
command construction and higher-level orchestration.

The facade and implementation are deliberately still separate:

- callers depend on `Rclone`, not implementation modules;
- compatibility changes can be handled at the facade boundary; and
- unit tests can exercise `RcloneImpl` command contracts without launching
  rclone.

`RcloneImpl` and the facade are still large and duplicate much of the public
operation surface. Treat that as a known refactoring boundary, not a pattern
to extend. New cohesive behavior should normally live in a focused module
and be exposed through a small facade method.

### Execution and configuration

`RcloneExec` is the adapter between operations and subprocess management.
Short-lived commands use `rclone_execute`; long-lived commands use `Process`.
Subprocesses receive argument lists and run with `shell=False`.

When a `Config` object is supplied, its text is written to a process-private
temporary directory. The config file is created with owner-only permissions
where the operating system enforces them, and cleanup is idempotent. Logged
command lines pass through `format_command`, which redacts password, token,
secret, authentication, and access/private-key values.

Configuration discovery uses this order:

1. an explicit path;
2. `RCLONE_CONFIG`; and
3. `rclone config paths`.

Failure to perform discovery raises `ConfigDiscoveryError`; a successful
search that finds no existing config returns `None`.

### Domain and feature modules

- `file.py`, `dir.py`, `remote.py`, `rpath.py`, and `types.py` hold the
  existing domain values.
- `fs/` provides local and remote filesystem adapters and `FSPath`.
- `http_server.py`, `mount.py`, and `process.py` own long-lived resources and
  provide context-manager APIs.
- `s3/` contains optional S3 operations and multipart upload strategies.
- `db/` contains optional database persistence.
- `cmd/` contains the installed console-script adapters.
- `runtime/` owns platform selection, verified downloads, safe extraction,
  hashing, permissions, caching, and executable resolution.

S3 and database dependencies are optional and imported lazily. Missing
packages raise `MissingOptionalDependencyError` with the extra to install.
Importing `rclone_kit` itself must not require optional extras, configure the
root logger, start a thread, or spawn a process.

## Bundled rclone lifecycle

The installed wheel is self-contained for its certified platform. The
executable passes through independent build-time and runtime checks:

1. `runtime/platform.py` selects one immutable `RcloneArtifact`.
2. `prepare_rclone_artifact.py` downloads or reuses the pinned release
   archive and verifies the archive SHA-256.
3. Safe extraction reads only the expected archive member.
4. The extracted executable is checked against a separate,
   repository-controlled executable SHA-256.
5. The executable, adjacent `.sha256` manifest, and rclone license are staged
   into an isolated source-tree copy.
6. The wheel packages only that platform's staged directory.
7. Distribution verification independently checks the executable against
   both its manifest and the repository-controlled digest.
8. At runtime, the bundled executable and manifest are verified and copied
   into the per-user application cache under an inter-process lock.
9. Cache replacement is atomic, and executable permission is applied before
   the cached path is returned.

`resolve_rclone_executable()` is fail-closed by default: after an explicit
path, it tries the bundled asset but does not use `PATH` or download unless
the caller opts in. The older `get_rclone_exe()` application adapter still
allows `PATH` lookup by default so a source checkout can use an installed
rclone. Verified download remains opt-in.

## Distribution policy

The project publishes one platform-specific, Python-ABI-independent wheel per
certified target:

- `py3-none-win_amd64`
- `py3-none-manylinux2014_x86_64`

The wheel contains a native executable as package data, not a Python
extension, so it must declare a platform but does not require a CPython ABI
tag. `Requires-Python >=3.13` remains the Python language-version boundary.
The in-tree `_build_backend.py` customizes the setuptools wheel tag, and
distribution verification checks the exact result.

Source distributions are not supported or published. A normal PEP 517 build
from an sdist has no certified-artifact staging step and could create an
incomplete wheel. Until that path is implemented and tested, releases are
wheel-only.

Do not use a bare `uv build` for release artifacts. The only supported build
entry point is `scripts/build_distribution.py`.

## Canonical local build

Prepare the locked environment:

```bash
uv python install
uv sync --locked --all-groups
```

Build on the same operating system and architecture as the requested target:

```bash
# Windows amd64
uv run python scripts/build_distribution.py --target windows-amd64 --out-dir dist

# Linux amd64
uv run python scripts/build_distribution.py --target linux-amd64 --out-dir dist
```

The target must match the current host; cross-building is not supported.
`--out-dir` must be empty or absent so stale artifacts cannot be mixed into
the build. Omit it to receive a unique temporary output directory.

The first build may access `downloads.rclone.org`. Verified archives are
cached per user, rclone version, platform, and expected digest. A cache hit
is hashed again before reuse.

The canonical command performs one atomic sequence:

1. resolves and validates the requested target;
2. copies only the wheel build inputs into a temporary source tree;
3. downloads, verifies, extracts, and stages one rclone artifact;
4. builds exactly one wheel;
5. runs all distribution-content checks;
6. creates a clean virtual environment using the pinned Python version;
7. installs the wheel with only default runtime dependencies;
8. runs the installed-wheel smoke test; and
9. prints the verified wheel path and SHA-256.

Staged executables never touch the tracked `src/` tree. Temporary staging and
the smoke environment are removed after success or failure.

If verification or the smoke test fails after wheel construction, treat any
wheel left in the output directory as unverified. Diagnose it, then use a new
empty output directory for the next run.

### Distribution verification

`scripts/verify_distribution.py` rejects a wheel unless it has:

- the exact `py3-none-<certified-platform>` tag;
- exactly the expected platform executable and no foreign executable;
- matching manifest, executable, and repository-controlled hashes;
- both the project license and bundled rclone license;
- resolvable console-script targets;
- a Python requirement that excludes versions below 3.13;
- no development-only runtime requirements; and
- no tests, caches, secrets, bytecode, or other denylisted files.

After collecting all platform wheels, verify the release set:

```bash
uv run python scripts/verify_distribution.py dist --require-complete-release-set
```

This additionally requires exactly one wheel for every entry in
`SUPPORTED_ARTIFACTS`, with no duplicate or unrecognized wheel.

The smoke test verifies the installed package rather than the source tree. It
checks import-time logging, thread, and child-process counts; resolves the
bundled executable through the application cache; runs `rclone version`; and
invokes every installed console script with `--help`. Poisoned proxy settings
provide best-effort network isolation during this test.

## CI pipeline

The CI dependency graph is:

```text
quality ---------> wheel-windows ----\
                 /                    \
tests-windows --/                      \
                                        > release-assembly
quality ---------> wheel-linux -------/
                 /
tests-linux ----/
```

- `quality` installs all dependency groups and optional extras, then checks
  formatting, Ruff, and Pyright.
- `tests-windows` and `tests-linux` run unit and integration tests with all
  extras on their native runners.
- each `wheel-*` job waits for quality and its matching platform tests,
  restores the verified-archive cache, runs the canonical build command, and
  uploads only its verified wheel;
- `release-assembly` downloads both wheels, verifies every wheel again,
  enforces the complete release set, prints SHA-256 digests, and uploads the
  assembled `release-dist` artifact.

Workflow permissions are read-only, jobs have timeouts, superseded runs are
cancelled, action revisions are pinned by commit SHA, uv is version-pinned,
and Python comes from `.python-version`.

CI assembles but does not publish a release. The tag-driven
`.github/workflows/release.yaml` workflow repeats the release gates and
publishes the verified release set through PyPI trusted publishing and the
approval-protected `pypi-release` environment. See `release_process.md`.

## Contribution workflow

### Set up the full development environment

```bash
uv python install
uv sync --locked --all-groups --all-extras
```

Use `uv` as the project, environment, dependency, build, and publishing
frontend. Change dependency declarations in `pyproject.toml`, then update
`uv.lock`; do not maintain a parallel requirements file.

### Run the development loop

Start with the narrowest relevant test, then run the standard local gates:

```bash
uv run ruff format .
uv run ruff check --fix .
uv run ruff format --check .
uv run ruff check .
uv run pyright _build_backend.py src tests scripts
uv run pytest tests/unit
uv run pytest tests/integration
```

The suites have different purposes:

- `tests/unit` must be deterministic, credential-free, and normally offline.
  It owns command contracts, parsing, security boundaries, runtime artifact
  behavior, build orchestration, and distribution verification.
- `tests/integration` resolves and runs a real rclone executable. It may need
  the verified-download cache or network access when run from a source
  checkout without rclone on `PATH`.
- `tests/cloud` is opt-in and mutates real remote storage. Use dedicated test
  credentials and the documented environment variables in `tests/helpers.py`.
  Mount tests additionally require WinFsp on Windows or FUSE and a usable
  unmount command on Linux. Cloud tests are not part of required pull-request
  CI.

Run the canonical platform build as well when changing packaging, the build
backend, runtime artifact code, entry points, dependencies, licenses, or
platform declarations.

### Code and test conventions

Follow `code_style.md`. In particular:

- preserve the public `Rclone` facade unless a deprecation path is provided;
- prefer small focused modules and pure command builders over adding more
  orchestration to `RcloneImpl`;
- use named constants instead of magic strings;
- do not mutate caller-owned argument lists;
- use explicit resource ownership and context managers;
- keep optional dependencies behind lazy imports;
- use named test constants and frozen case dataclasses for identical
  parametrized control flow; and
- add regression tests before changing an established contract.

Do not commit or push unless explicitly asked. Keep one logical change per
commit and follow the authorship and commit-message rules in `code_style.md`.

## Guide for common changes

### Change a public operation

1. Capture current command vectors, return values, and failure behavior with
   a unit test using a fake execution boundary.
2. Put new command construction in a pure helper or focused operation module.
3. Keep raw subprocess behavior in `RcloneExec`, `Process`, or
   `rclone_execute`.
4. Update the `Rclone` facade without exposing `RcloneImpl` as public API.
5. Test empty inputs, explicit `False` options, caller-owned arguments,
   credential redaction, and subprocess failure.

### Change runtime artifact handling

Keep `runtime/platform.py` as the only artifact catalog. Downloads must be
pinned, independently hash-verified, safely extracted, and atomically cached.
Never add an unverified "latest" URL or silently fall back to a downloaded or
`PATH` executable.

Any change here needs focused unit tests for digest mismatch, unsafe archive
members, cleanup, permissions, cache replacement, and concurrent
installation, plus a canonical wheel build on every affected platform.

### Bump rclone or add a platform

1. Update the artifact data and `SUPPORTED_ARTIFACTS` in
   `runtime/platform.py`.
2. Obtain the archive digest from the upstream release checksums.
3. Independently hash the executable extracted from a verified archive.
4. Add platform normalization and cache/build tests.
5. Ensure `_build_backend.py` emits the intended exact wheel tag.
6. Add a native test and wheel job in CI.
7. Build, verify, smoke-test, and assemble the complete release set.
8. Update supported-platform documentation and the release record.

Adding a target is not complete when only the download works; installation,
tagging, runtime resolution, CI ownership, and release-set verification must
all agree.

### Change dependencies or entry points

Keep default dependencies minimal. Put feature-specific packages in an
optional extra and preserve actionable lazy-import errors. Sync the lockfile,
run quality checks with `--all-extras`, and build a wheel to verify metadata.

For a new console script, add its `[project.scripts]` entry and ensure its
callable supports `--help` in a minimal installed-wheel environment. The
distribution verifier and smoke test discover console scripts dynamically.

## Improvement roadmap

The build pipeline is substantially complete. Source improvements were
delivered as focused correctness, security, dependency, logging, cleanup, and
test changes; the broad architectural phases were not completed. Improve
them incrementally:

The error model phase is done: `rclone_kit.exceptions` now holds a typed
`RcloneKitError` hierarchy (`FilesystemError`, `ConfigParseError`,
`RcloneCommandError`, `HttpFetchError`, `MergeStateError`, `S3MergeError`,
`S3UploadError`), every internal call site that used to return `Exception`
as data now raises, and the transitional `_raise_if_exception` bridge has
been removed along with its last call site.

The resource ownership phase is done except for one item folded into the S3
multipart phase below: `Process` and `rclone_execute` no longer register a
per-instance/per-call `atexit` closure (replaced by a single import-time
registration draining a `WeakSet` registry, mirroring `mount_util.py`'s
existing mount registry); `FilePart` prunes its exit-cleanup list and its
`dispose()` is idempotent; `scan_missing_folders` no longer leaks a blocked
background thread when a caller stops iterating early; `FSWalkThread` and
`FSWalker` gained an idempotent `close()` reachable outside the
context-manager protocol, which also fixed a latent double-`Thread.start()`
bug; `upload_parts_resumable` reuses the same registry pattern instead of a
per-call `atexit` closure; `RemoteFS` and `DB` gained context-manager support
to match their sibling resource owners; and `fs/walk.py`'s module-level
thread pool is now documented as an intentional process-lifetime singleton
rather than an undocumented oversight. `WriteMergeStateThread` and
`S3MultiPartMerger` in `upload_parts_server_side_merge.py` still need an
explicit `close()`/`stop()` and confirmed ownership; that is deferred to the
S3 multipart phase since it touches the same state machine.

The S3 multipart phase is done for its ownership and evidence goals; the
underlying upload algorithms were intentionally not rewritten. Rather than
force a single state machine across three structurally different
strategies, the ownership gap was closed and the implicit states each
strategy already had were named and tested: `WriteMergeStateThread` gained
an idempotent `close()` that always sends its end-of-stream sentinel before
joining (fixing a real bug where `_do_upload_task`'s
`executor.shutdown(..., cancel_futures=True)` on a retry-exhausted part
copy could cancel the not-yet-started sentinel task, orphaning the writer
thread on `queue.get()` forever); `S3MultiPartMerger` gained a matching
`close()`/context manager, and `merge()` now closes the write thread in a
`finally` around the upload so it happens on both success and failure;
`close()` raises `S3MergeError` on a join timeout rather than warning,
since a merge whose state never reached `merge.json` is a correctness
signal, not a best-effort cleanup miss. Two dead `is`/`is not EndOfStream`
identity checks (comparing an instance to the class) were fixed to
`isinstance`. New fake-S3 regression tests cover every required-evidence
scenario: resume (`_begin_or_resume_merge` from a valid prior state),
corruption (the same function falling back to a fresh merge on a malformed
one), retry exhaustion and worker failure (a real retry-exhausted part
copy through the actual `ThreadPoolExecutor` path), completion failure
(every part succeeding but `complete_multipart_upload` itself failing), and
cleanup (the write thread closed on every one of those paths). The
resumable strategy's "finished part" tracking (a directory listing, not a
persisted state object like `MergeState`) was deliberately left as-is: it
already defers to `rclone`'s own listing as the source of truth, and
introducing a parallel persisted-state mechanism there without a
demonstrated need would just duplicate `MergeState`'s design. A
`PartMergeState` naming/documentation pass was considered speculative and
skipped.

| Area | Current constraint | Preferred next step | Required evidence |
|---|---|---|---|
| Public facade | `Rclone` and `RcloneImpl` remain large and duplicate the operation surface. | Extract pure command builders and cohesive listing, transfer, configuration, serve, mount, and S3 services while keeping `Rclone` stable. | Characterization tests and an API compatibility snapshot. |
| Paths and filesystems | Local paths, rclone paths, and strings still overlap. | Introduce immutable local/rclone path values and one documented `FS.ls` contract. | Identical remote-path behavior on Windows and Linux. |
| Typing and linting | Legacy rule families remain globally ignored and Pyright is not strict. | Make new modules strict, then remove one narrow ignore family per behavior-preserving change. | Quality gates pass with a smaller ignore surface and no broad `Any` escape hatches in changed code. |
| Test isolation | Cloud tests retain legacy `unittest` setup and disabled scenarios. | Move shared setup to pytest fixtures and replace disabled tests with deterministic fakes where possible. | Unit tests run offline and in arbitrary order; external suites are explicitly opt-in. |
| Release publication | CI assembles verified wheels but does not publish or attest them. | Configure PyPI trusted publishing, an approval-protected environment, a tag-driven publish job, and artifact attestations. | Only a verified `release-dist` artifact can reach the publish job. |
| Build isolation | Smoke tests poison proxies but do not enforce network denial. | Run them in a network-disabled container or namespace where supported. | A deliberate network attempt fails while the bundled executable still runs. |
| Source distributions | An sdist cannot yet build a complete certified wheel. | Keep wheel-only releases, or add a verified artifact input/download hook and test sdist-to-wheel builds on every target. | A built-from-sdist wheel passes the same verifier and smoke test. |

Keep improvement pull requests small. Establish the contract with tests,
change one boundary, preserve compatibility, and remove the old path only
after all callers have migrated.

## Pull request checklist

- [ ] The change is one coherent behavior or refactoring.
- [ ] Public compatibility is preserved or has a documented deprecation.
- [ ] Unit tests cover success, failure, cleanup, and platform edge cases.
- [ ] Optional features still import without their extras installed.
- [ ] Commands use argument lists and diagnostics redact credentials.
- [ ] Formatting, Ruff, Pyright, unit tests, and integration tests pass.
- [ ] A canonical wheel build passes when distribution behavior changed.
- [ ] Documentation and authoritative constants agree.
- [ ] No generated executable, secret, cache, or build artifact is tracked.
