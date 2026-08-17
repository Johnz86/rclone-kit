# Implementation and build pipeline

## Purpose and status

This is the current maintainer guide for the `rclone-kit` implementation,
test strategy, build pipeline, and safe contribution workflow. See
"Improvement roadmap" below for what remains: typing/linting depth,
release-pipeline refinement, build isolation, and source distributions.

The authoritative configuration remains in the code:

- `pyproject.toml` defines Python support, dependencies, entry points, build
  backend, and quality-tool configuration.
- `.python-version` pins the Python patch release used by maintainers and CI.
- `uv.lock` pins the resolved development environment.
- `src/rclone_kit/runtime/platform.py` defines the supported operating
  systems and machine architectures; `runtime/native_platform.py` maps them
  to certified `NativeTarget`s (wheel platform tag, library/executable
  filenames).
- `native/toolchain.toml` pins the rclone fork URL and branch names, the Go
  version, and the Windows/Linux compiler toolchains a native build uses;
  the fork *commit* is pinned by the `native/rclone` submodule gitlink, not
  by this file.
- `.github/workflows/ci.yml` defines the required CI graph.
- `docs/release_process.md` defines release recording and publication.

Update this guide when any of those contracts changes.

## Implementation overview

The main execution path is:

```text
Application / console script
        |
        v
Rclone client (rclone_kit/client.py)
        |
        v
focused operation modules (rclone_kit/operations/*_embedded.py)
        |
        v
RcClient / RcloneRuntime (rclone_kit/rc/, rclone_kit/native/runtime.py)
        |
        v
librclone_kit shared library (ctypes, in-process)
        |
        v
local or remote storage
```

### Public API and operations

`rclone_kit.Rclone` is the stable public client. It exposes listing, copying,
deletion, HTTP serving, mounting, filesystem, database, and S3 operations.
`rclone_kit.__init__` only re-exports this class and the curated domain
values.

The client owns one `RcloneRuntime` (the initialized native library handle)
and an `RcClient` built on top of it, and delegates command-oriented behavior
to focused operation modules under `operations/`. Tests provide fake RC
clients without subclassing a production class. Domain values use small
access protocols for the convenience methods that still retain a client
reference.

### Execution and configuration

`RcClient` (`rclone_kit/rc/client.py`) is the adapter between operations and
the native library: every operation issues one or more RC calls
(`rc/list`-shaped methods, e.g. `sync/copy`, `operations/stat`) directly
in-process through `ctypes`, never a subprocess. Long-lived resources
(`JobHandle`, `MountHandle`, `ServeHandle`) wrap an RC job or handle rather
than a child process.

When a `Config` object is supplied, its text is materialized to a
process-private temporary file at most once per `Config` instance and passed
to `RcloneRuntime.initialize()` as the config path. The config file is
created with owner-only permissions where the operating system enforces
them, and cleanup is idempotent (process-exit, via `atexit`).

Configuration discovery (`config_discovery.py:find_conf_file_embedded()`)
uses this order:

1. an explicit path;
2. `RCLONE_CONFIG`; and
3. a `config/paths` RC call against a throwaway, uninitialized-config
   `RcloneRuntime`.

Failure to perform discovery raises `ConfigDiscoveryError`; a successful
search that finds no existing config returns `None`.

The native library can be initialized at most once per OS process:
`RcloneKitFinalize` is best-effort cleanup, not a reset, so a second
`Rclone(...)` construction in the same process fails with
`NativeAlreadyInitializedError` unless it is handed an already-initialized
runtime. `native/runtime.py:shared_runtime()` is the supported way to give
several `Rclone` clients (one per request, one per tenant) the same
process: it lazily creates and initializes exactly one process-wide
`RcloneRuntime`, and every call after the first returns that same instance
regardless of the arguments passed. Construct each client as
`Rclone(per_client_conf, runtime=shared_runtime())`; each keeps its own
job/serve/mount tracking and can `close()` independently, and clients
sharing a runtime also share its one immutable config path (a call-dispatch
convenience, not a tenancy boundary). This same rule makes
`find_conf_file_embedded()` a footgun if misused: its own throwaway
`RcloneRuntime` is initialized and then `close()`d, but `close()` never
frees the process's one-shot initialization slot - calling this function in
a process that will later construct a real embedded `Rclone(...)` client
permanently exhausts that slot and makes the later `initialize()` fail. Use
it only when no real embedded runtime is initialized afterward in the same
process.

`RcloneRuntime.call()` does not serialize concurrent RC dispatch against
other `call()`s - only `initialize()`/`close()` and the in-flight call
count are guarded by a lock, so two operations can run their RC calls
concurrently on one runtime. `close()` waits for every in-flight call to
finish rather than interrupting one (RC dispatch has no cancellation
mechanism to interrupt it with). This is why a slow call - e.g. an
`ls_stream()` pull awaiting new items - does not starve an unrelated
job-status check or cancellation the way full serialization would.

That wait is unbounded by design: finalizing while a call is still
dispatched would tear down state the call is using, which is a
process-level crash rather than a recoverable error, so no timeout could
safely give up and finalize anyway. Instead the wait reports itself,
logging every 10s how many calls are still outstanding, so a slow drain
is diagnosable rather than a silent hang.

No library code writes into the current working directory.
`chunk_store.get_staging_root()` is the single place that decides where
large temporary files go - byte-range chunk downloads, S3 multipart upload
chunks, and the opt-in upload log. It defaults to an `rclone-kit`
subdirectory of the OS temporary directory and honours `RCLONE_KIT_TMP_DIR`,
re-reading the environment on every call so a deployment or a test can
move the location after import. Temporary config files go under the OS
temporary directory too, via `tempfile.mkdtemp`.

### Resource ownership and exit cleanup

Every type that owns an operating-system resource exposes an idempotent
teardown method, and the ones used in a scoped way also implement the
context-manager protocol. The method name splits by lineage rather than by
accident: `JobHandle`, `DB`, `FSWalkThread`, `FSWalker`, and
`WriteMergeStateThread`/`S3MultiPartMerger` in
`s3/multipart/upload_parts_server_side_merge.py` expose `close()`, while
`MountHandle`, `ServeHandle`, and `RemoteFS` expose `dispose()` - the name
`Rclone.close()` itself calls when it drains its tracked resources. Either
way the method is reachable without `with`, so a caller that owns the
object across a wider scope is not forced into the protocol.

Exit-time cleanup goes through `util.make_atexit_registrar`, which
registers exactly one `atexit` handler per module and drains a registry of
live objects. A closure registered per instance or per call would instead
pin its captured arguments for the rest of the process and grow without
bound. `file_part.py` and `util.py` use it.
`s3/multipart/upload_parts_resumable.py` instead registers its own single
module-level `atexit` handler directly, and does so at import time rather
than lazily on first use, which is correct there because the module is
only ever imported function-locally at its one real call site - import and
first use already coincide.

`fs/walk.py`'s module-level `ThreadPoolExecutor` is an intentional
process-lifetime singleton, shared by every `FSPath` walk and sized once
at import from `FS_WALK_THREAD_MAX_BACKLOG`.

`walk()` and `scan_missing_folders()` share one background-thread
lifecycle, `background_producer.iter_background_producer`: the traversal
runs on a daemon thread that feeds a bounded queue and always ends by
putting a `None` sentinel, while the caller sees a generator. That module
owns the parts easy to get wrong in a copy - draining the queue when a
consumer stops iterating early, so the producer is not left blocked
forever on a queue nobody reads; joining with a timeout and reporting an
overrun; re-raising the producer's exception from the consumer's own call
rather than signalling it with `_thread.interrupt_main()`; and letting a
consumer's `KeyboardInterrupt` propagate instead of returning a silently
truncated listing.

### Domain and feature modules

- `file.py`, `dir.py`, `remote.py`, `rpath.py`, and `types.py` hold the
  existing domain values.
- `fs/` provides local and remote filesystem adapters and `FSPath`.
- `http_server.py`, `mount_handle.py`, `serve_handle.py`, and `job.py` own
  long-lived resources and provide context-manager APIs.
- `s3/` contains optional S3 operations and multipart upload strategies.
- `db/` contains optional database persistence.
- `cmd/` contains the installed console-script adapters.
- `runtime/` owns operating-system/architecture selection
  (`platform.py`/`native_platform.py`) and hashing; `native/` owns native
  library resolution (`library.py`), the `ctypes` runtime binding
  (`runtime.py`), and build-info reporting (`build_info.py`).

Rclone paths (`remote:bucket/path`) are parsed with `PurePosixPath`, never
`pathlib.Path`. `Path` resolves to `WindowsPath` on Windows, which treats a
literal `\` inside a segment - a valid character in many remote object
keys - as a directory separator, silently splitting one object name into
two path components; the same code is `PosixPath` on Linux and shows no
symptom there. `group_files`, `Dir`/`File`'s path math, `FileItem.from_json`,
`RemoteFS._to_remote_path`, and `FSPath` all follow this rule. `FSPath` is
shared by `RealFS`, which legitimately wants native `Path` semantics, and
`RemoteFS`, which does not, so it branches on its `FS` type in
`_pure_path()`.

`RealFS.ls` returns full path strings while `RemoteFS.ls` returns bare
names (directories keep a trailing `/` marker). That asymmetry is
load-bearing rather than an inconsistency to clean up: `FSPath.__truediv__`
and `fs_walk`'s `current / name` join work uniformly across both only
because `pathlib`'s `/` discards its left side when the right side is
itself absolute. `FS.ls`'s abstract method documents the contract.

S3 and database dependencies are optional and imported lazily. Missing
packages raise `MissingOptionalDependencyError` with the extra to install.
Importing `rclone_kit` itself must not require optional extras, configure the
root logger, start a thread, or spawn a process.

`s3/multipart/` holds three structurally different upload strategies -
inline, resumable, and server-side merge - rather than one state machine,
because their shapes genuinely differ. Only the server-side merge persists
progress as an object: `MergeState` in `merge.json`, written by a
`WriteMergeStateThread` fed over a queue. That thread's `close()` always
sends the end-of-stream sentinel itself before joining, so a caller cannot
orphan the writer by forgetting to; `_do_upload_task`'s
`executor.shutdown(..., cancel_futures=True)` on a retry-exhausted part
copy would otherwise cancel a not-yet-started sentinel task and leave the
writer blocked on `queue.get()` forever. `merge()` closes the thread in a
`finally`, so it happens on success and failure alike, and `close()`
raises `S3MergeError` on a join timeout rather than warning, because a
merge whose state never reached `merge.json` is a correctness signal, not
a best-effort cleanup miss. The resumable strategy has no equivalent
persisted state by design: it treats rclone's own listing of the parts
directory as the source of truth for which parts finished.

### Diagnostics and the error taxonomy

Every runtime diagnostic goes through `logging`. `T201` is enforced under
`src/rclone_kit/`, with narrow per-file exceptions for the console scripts,
`scripts/`, the tests, and the single deliberate `print` implementing
`Rclone.print()`, so a stray `print` in library code is a lint error.
`warnings` is reserved for the one thing it is actually right for:
`RemoteFS.mkdir`'s caller-misuse report, which tells a programmer they
called something their backend cannot do. Per-item runtime diagnostics must
not use it, because the warnings channel deduplicates on
`(message, category, module, lineno)` and would fire once per process and
then drop everything after.

`RcloneKitError` is the single root of the hierarchy: the per-subsystem
base types `RcCallError` (a failed RC call), `NativeError` (an ABI-level
fault), `RcJobNotFoundError`, and `RcloneRuntimeError` (a platform or
native-artifact fault) all subclass it, so the `except RcloneKitError`
boundary handler `production_usage.md` recommends genuinely catches a
failed RC call. `MissingOptionalDependencyError` is the one deliberate
exclusion: it subclasses `ImportError` because a missing extra is a
deployment packaging fault, and belongs in a permanent-failure branch
ahead of the catch-all. The package root exports every type in
`exceptions.py` plus each subsystem's base type; narrower subsystem
subclasses (the `NativeError` family, `RcJobNotFoundError`,
`RcloneRuntimeError`'s two subclasses) stay in their defining modules, so
a boundary handler catches the base rather than enumerating them. The
defining modules stay importable, but the package root is the supported
import path.

### Job handles and the retry-aware copy endpoint

rclone's own RC `sync/copy` handler calls `CopyDir` once - it never enters
the CLI's `cmd.Run`, which owns the high-level retry loop, fatal/no-retry
error classification, and retry-sleep behavior. A bare `sync/copy` job
therefore silently drops from the CLI's multiple high-level attempts to
one. `start_copy()` and every method built on it (`copy()`, `copy_dir()`,
`copy_remote()`, the partitioned `copy_files()`) instead call
`rclonekit/copy`, a small fork-owned RC endpoint
(`native/rclone/librclone/rclonekit/rc/copy.go`) that reproduces `cmd.Run`'s
retry loop scoped to the calling job's own accounting group, so concurrent
jobs never share retry/error state.

rclone deletes a finished job's `job/status` record after
`rc_job_expire_duration` (60s by default, checked every 10s). `_JobMonitor`
(`job.py`) exists to poll and cache every started job's terminal status and
stats before that expiry window can lose them, so a caller that calls
`JobHandle.wait()` long after a transfer actually finished still gets the
real result. Job identity is validated on both `jobid` and `executeId`:
rclone restarts job IDs from 1 after a process restart, so `executeId` is
what actually distinguishes an old job from a coincidentally reused ID; a
mismatch raises `JobIdentityError` rather than silently trusting a stale ID.

There is no fork-owned `rclonekit/sync` or `rclonekit/move`, so
`sync()`/`start_sync()` and `move()`/`start_move()`/`move_to()` call
upstream `sync/sync`/`sync/move` directly. The underlying operation runs
exactly once, `OperationResult.attempts` is always empty for them, and
they expose no `retries` parameter, because `_config.Retries` is read only
by the command-level loop they do not have. Per-file `low_level_retries`
still applies, since it is enforced inside the operation. The loop is
deliberately not reimplemented in Python: rclone resets its accounting
group's error state between attempts, which an out-of-process caller
cannot do, so a naive retry would double-count stats and misattribute an
abandoned attempt's errors.

One `_JobMonitor` tick costs one RC round-trip no matter how many jobs it
tracks: it polls them all through `job/batch` (`RcBatchStatusClient`), so
the effective poll interval does not degrade with partition count. A
whole-call failure there disables batching permanently and falls back to
one `job/status` per job on the same tick. `job/list` is not used, because
`librclone.RPC` registers every RC call as a job, which would size the
response by all of the process's bookkeeping jobs rather than by the
tracked ones.

Three poll outcomes are permanent rather than transient, and settle the
record terminally as `JobState.LOST`: rclone's own record expired
(`JobExpiredError`), the job id was reused by a restarted rclone
(`JobIdentityError`), and the polling runtime was closed
(`JobRuntimeClosedError` - `RcloneRuntime`'s closed flag is a one-way
latch). An unsettled record would block `wait()` indefinitely and burn the
whole `Rclone.close()` shutdown deadline. `NativeNotInitializedError` stays
in the transient branch: a job being polled at all implies an initialize
that already succeeded. `LOST` covers all three because `JobState` is
public API and they are the same fact to a caller - terminal, outcome
unknowable; which one it was stays in `JobStatus.error` and in the
`OperationError` subclass `wait()` re-raises.

Progress is pulled, never pushed, and that is a considered choice rather
than a missing feature. rclone has nothing to push from: its own
`--progress` flag is a 500ms ticker goroutine (`cmd/progress.go`)
repainting from the same mutex-protected `StatsInfo` that `core/stats`
reads on demand, the Prometheus exporter polls that same structure again,
and the finest granularity the accounting layer produces is a ~1s-averaged
speed figure. A push channel would also be the first reverse-direction
call in the ABI - Go invoking a C function pointer needs a cgo trampoline,
since cgo only makes the C-calls-Go direction free - and would require an
`RCLONEKIT_ABI_VERSION` bump with negotiation between older and newer
library/binding pairs. So `JobHandle.watch()` is a generator sleeping
between `stats()` calls and `on_progress()` runs that generator on its own
thread (`progress.py`, shared with `PartitionedJobHandle` through the
narrow `ProgressSource` protocol); neither adds anything below the
`_JobMonitor` boundary.

`check()` is the one operation that is not a job. It is a direct
synchronous `operations/check` call returning a frozen `CheckResult`,
because the report *is* the RC output and the job types deliberately
surface only `attempts` from that output. It therefore has no `JobHandle`
and cannot be cancelled or progress-polled; it blocks nothing but its own
caller, since `RcloneRuntime.call` holds no lock for a call's duration.

`copy_files()`/`delete_files()` write each partition's file list to a
temporary `_filter.FilesFrom` file rather than passing paths inline - RC has
no way to hand rclone an in-memory file list. A path in that list that does
not exist is not an error: the underlying walk simply never visits it,
matching `rclone copy --files-from`'s own CLI behavior, so a typo'd path is
silently skipped rather than raising.

## Bundled native library lifecycle

The installed wheel is self-contained for its certified platform. The
`librclone_kit` shared library passes through independent build-time and
runtime checks - see `native/README.md` for the full toolchain detail this
section summarizes:

1. `scripts/native/build.py` builds `librclone_kit` (`-buildmode=c-shared`)
   and a diagnostic `rclone` executable from the pinned `native/rclone`
   submodule commit, using the Go/C toolchain recorded in
   `native/toolchain.toml`.
2. The build runs a focused native smoke test (`scripts/native/smoke.py`)
   directly through `ctypes` and writes `native-manifest.json` (fork commit,
   toolchain identity, per-output SHA-256 digests) and `SHA256SUMS`.
3. `scripts/build_distribution.py` stages the library, its manifest, the ABI
   header, and the rclone license into the wheel's package-data directory
   (excluding the diagnostic executable and smoke results).
4. The wheel packages only that platform's staged directory.
5. `scripts/verify_distribution.py` independently checks the packaged
   library against its manifest's recorded digest.
6. At runtime, `native/library.py:resolve_library_path()` resolves the
   packaged library path and re-verifies its SHA-256 against the same
   manifest before it is ever loaded - no cache directory, no copy, no
   subprocess.

`resolve_library_path()` is fail-closed: an explicit path or the
`RCLONE_KIT_LIBRARY` environment override is tried first, then the packaged
wheel asset; anything else raises `LibraryNotFoundError`. A digest mismatch
raises `LibraryVerificationError` rather than loading an unverified library.

## Distribution policy

The project publishes one platform-specific, Python-ABI-independent wheel per
certified target:

- `py3-none-win_amd64`
- `py3-none-manylinux2014_x86_64`

The wheel contains a native shared library (`librclone_kit.dll`/`.so`) as
package data, not a Python extension, so it must declare a platform but does
not require a CPython ABI tag. `Requires-Python >=3.13` remains the Python
language-version boundary.
The in-tree `_build_backend.py` customizes the setuptools wheel tag, and
distribution verification checks the exact result.

Source distributions are not supported or published. A normal PEP 517 build
from an sdist has no certified-artifact staging step and could create an
incomplete wheel. Until that path is implemented and tested, releases are
wheel-only.

Do not use a bare `uv build` for release artifacts. The only supported build
entry point is `scripts/build_distribution.py`.

## Canonical local build

Prepare the locked environment, the `native/rclone` submodule, and the
native toolchain (Go, plus llvm-mingw on Windows or a manylinux2014
container on Linux - see `native/README.md` for the exact, verified setup):

```bash
uv python install
uv sync --locked --all-groups
git submodule update --init native/rclone
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

The native library build accesses no network URL for rclone itself - it
compiles `librclone_kit` from the pinned `native/rclone` submodule commit
using the local Go/C toolchain. `scripts/native/verify_submodule_pin.py`
(run in CI, and available locally) confirms that pinned commit is actually
fetchable from the configured fork remote.

The canonical command performs one atomic sequence:

1. resolves and validates the requested target;
2. copies only the wheel build inputs into a temporary source tree;
3. builds `librclone_kit` from the pinned `native/rclone` submodule and
   stages the library, its manifest, the ABI header, and the rclone license;
4. builds exactly one wheel;
5. runs all distribution-content checks;
6. creates a clean virtual environment using the pinned Python version;
7. installs the wheel with only default runtime dependencies;
8. runs the installed-wheel smoke test; and
9. prints the verified wheel path and SHA-256.

Staging happens in an isolated copy of the source tree, so the tracked `src/`
is byte-identical before and after a build. Temporary staging and the smoke
environment are removed after success or failure.

If verification or the smoke test fails after wheel construction, treat any
wheel left in the output directory as unverified. Diagnose it, then use a new
empty output directory for the next run.

### Distribution verification

`scripts/verify_distribution.py` rejects a wheel unless it has:

- the exact `py3-none-<certified-platform>` tag;
- exactly the expected platform's native library and no foreign platform's;
- matching manifest and digest hashes for the packaged library;
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
`SUPPORTED_NATIVE_TARGETS`, with no duplicate or unrecognized wheel.

The smoke test verifies the installed package rather than the source tree. It
checks import-time logging, thread, and child-process counts; resolves and
loads the bundled native library directly; exercises the native ABI through
`rclone_kit.native`; and invokes every installed console script with
`--help`. Poisoned proxy settings provide best-effort network isolation
during this test.

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
- `tests-windows` and `tests-linux` run `tests/unit` with all extras on
  their native runners (no native build needed - `tests/unit` uses fake RC
  bindings, not the real library).
- each `wheel-*` job waits for quality and its matching platform tests, then:
  checks out `native/rclone` (`submodules: true`), verifies the pinned
  commit is fetchable from the fork remote
  (`scripts/native/verify_submodule_pin.py`), installs the native toolchain
  (Go/llvm-mingw/WinFsp via `actions/setup-go` + cached downloads on
  Windows; a `manylinux2014` container driven by `docker run`/`docker exec`
  on Linux, mirroring `native/README.md`'s manual recipe rather than GHA's
  `container:` job directive, since that would run JS-based actions inside
  the container's old glibc), builds the native library
  (`scripts/native/build.py --target <target> --profile production`), runs
  `tests/native` against the real library, runs the canonical build command
  (`scripts/build_distribution.py`, which builds the library a second time
  in its own isolated temp dir by design), and uploads only its verified
  wheel;
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
uv run pytest tests/native
```

Run `tests/unit` and `tests/native` as **separate** `pytest` invocations, not
combined: `tests/cloud/conftest.py` and `tests/native/conftest.py` are both
importable as the bare module name `conftest` (pytest's default `prepend`
import mode inserts each conftest's own directory into `sys.path`, and
neither directory is a package), so a combined run can have one sibling's
`conftest` silently win for both via Python's module cache.

The suites have different purposes:

- `tests/unit` must be deterministic, credential-free, and normally offline.
  It owns command contracts, parsing, security boundaries, runtime artifact
  behavior, build orchestration, and distribution verification, using fake
  RC bindings rather than the real native library.
- `tests/native` exercises the real, built `librclone_kit` library directly
  (DLL/SO-backed integration tests). Skips automatically when no built
  library exists at `build/native/<target>/` - run
  `scripts/native/build.py --target <target> --profile production` first
  (see `native/README.md`). This is the suite that covers real
  library-backed behavior; there is no subprocess-level integration suite,
  because there is no executable to drive.
- `tests/cloud` is opt-in and mutates real remote storage. Use dedicated test
  credentials and the documented environment variables in `tests/helpers.py`.
  Mount tests additionally require WinFsp on Windows or FUSE and a usable
  unmount command on Linux. Cloud tests are not part of required pull-request
  CI.
- `tests/live` exercises the real implementation end-to-end against actual,
  fully configured remotes, rather than the parametrized environment-variable
  providers `tests/cloud` covers. Each backend gets its own subdirectory,
  marker, and config file, so any one of them can be run independently of the
  others as more providers are added - a contributor with only one backend
  configured never needs the other's config file present. Neither is part of
  required pull-request CI.

  - `tests/live/s3` covers a live Ceph/S3-compatible backend. It requires
    `rclone-test.conf` at the repository root - gitignored, never committed -
    with a `[kinit-s3]` remote (see the exact format in
    `tests/live/s3/conftest.py`'s missing-config message). Run it explicitly
    with:

    ```bash
    uv run pytest tests/live/s3 -m live_s3
    ```

  - `tests/live/gdrive` covers a live Google Drive remote and proves the
    same general (non-S3-optimized) API against a structurally different
    backend - Drive has no bucket concept, stricter per-user rate limits,
    and real hierarchical folders instead of S3-style prefixes. It requires
    `rclone-gdrive.conf` at the repository root (see
    `tests/live/gdrive/conftest.py`'s missing-config message for the setup
    command; PowerShell needs the `&` call operator for a quoted executable
    path). Run it explicitly with:

    ```bash
    uv run pytest tests/live/gdrive -m live_gdrive
    ```

  - `tests/live/gdrive_authorization` covers `Rclone.authorize()` itself -
    the process of *becoming* configured, not operations against an
    already-configured remote like `tests/live/gdrive` covers. It needs no
    config file and no Google Cloud Console setup (it uses rclone's own
    built-in shared client_id, the same as plain interactive `rclone config
    create`), but does need a native library built locally
    (`uv run python scripts/native/build.py --target <target> --profile
    production`; `--target` is required, e.g. `windows-amd64`) and, unlike
    every other suite here, a real human present: it blocks mid-run waiting
    for someone to approve access in a real browser, so it can never be
    scripted further without violating the provider's terms of service.
    Uses its own remote name (`gdrive-authtest`) and config file
    (`rclone-gdrive-authtest.conf`) so it never touches
    `tests/live/gdrive`'s remote or file. Run it explicitly with:

    ```bash
    uv run pytest tests/live/gdrive_authorization -m live_gdrive_authorization
    ```

  Every suite's `pytest_collection_modifyitems` deselects its own tests
  unless the caller passes its matching `-m live_s3`/`-m live_gdrive`/
  `-m live_gdrive_authorization`, so a bare `pytest` run (which would
  otherwise sweep every directory in via `testpaths`) collects zero tests
  from any of them. Once a suite's marker is explicitly requested, it
  hard-stops the session with `pytest.exit()` if its own prerequisite
  (config file, or - `gdrive_authorization` only - a built native library)
  is missing, rather than letting every test fail individually. All writes
  and deletes are scoped to a dedicated bucket (`rclone-kit-live-test`, S3)
  or folder (`rclone-kit-live-test`, Drive); nothing else on either remote
  is ever touched, and `gdrive_authorization`'s own remote/config are
  entirely separate from `tests/live/gdrive`'s to begin with.

  Each suite's `conftest.py` module is literally named `conftest`, same as
  its siblings' - test files must reach shared constants through the
  `live_remote_name`/`live_test_root`/`live_test_bucket` fixtures each
  `conftest.py` defines, never through `from conftest import ...`. A plain
  Python import resolves through the process-wide `sys.modules["conftest"]`
  cache rather than pytest's own per-directory conftest resolution, so once
  multiple suites are loaded in the same session - which a bare `pytest`
  run does for collection alone, even when all end up deselected -
  whichever sibling's `conftest.py` happened to import first silently wins
  for all of them (confirmed live: it swapped `LIVE_REMOTE` between the s3
  and gdrive suites). This constraint applies to every provider - and every
  new suite for an existing one - added under `tests/live/`.

  Two behaviors of the generic (non-S3) listing path - the path the Drive
  suite actually exercises - follow from the same fact: `ls()` on a
  nonexistent leaf path fails outright on a backend with real hierarchical
  directories (Drive, SFTP, local, ...) instead of returning an empty
  listing the way it does for an S3-style prefix, since S3 has no real
  "directory" to fail to resolve.
  `fetch_stat_embedded()`/`fetch_size_file_embedded()`
  (`operations/listing_ops_embedded.py`) both go through `_stat_item`,
  which raises the documented `FileNotFoundError` when `operations/stat`
  returns no `item`; `check_exists_embedded()` catches that same
  `FileNotFoundError` and returns `False` -
  `tests/live/gdrive/test_live_gdrive_ls_and_stat.py`'s missing-object
  tests exercise this directly. `scan_missing_folders()` propagates the
  listing failure itself (the `RcCallError` from `operations/list`): its
  background thread collects it and the generator re-raises it to the
  caller, so a dst root that doesn't exist at all surfaces as that error
  rather than an empty (wrong) result.
  `test_scan_missing_folders_finds_the_nested_directory_before_any_copy`
  routes around it by writing a sibling file first so the dst root exists,
  and says so in its own docstring.

Run the canonical platform build as well when changing packaging, the build
backend, runtime artifact code, entry points, dependencies, licenses, or
platform declarations.

### Code and test conventions

Follow `code_style.md`. In particular:

- change the public `Rclone` contract deliberately, updating its tests and
  the documentation that describes it in the same change;
- prefer small focused modules and pure command builders over adding more
  orchestration to `Rclone`;
- use named constants instead of magic strings;
- do not mutate caller-owned argument lists;
- use explicit resource ownership and context managers;
- keep optional dependencies behind lazy imports;
- use named test constants and frozen case dataclasses for identical
  parametrized control flow; and
- add regression tests before changing an established contract.

`tests/cloud` builds its credentials once, in `tests/cloud/conftest.py`:
`build_do_spaces_config()` builds the DigitalOcean Spaces `Config` and
calls `pytest.skip` when `DIGITAL_OCEAN_SPACES_ENV_VARS` are missing. The
session-scoped `cloud_runtime` initializes one process-wide
`RcloneRuntime` from it - the native ABI permits initializing a runtime
exactly once per process - and `cloud_rclone` hands out `Rclone` clients
sharing that runtime and its already-configured `dst:` remote. Every
client-using file injects one as `self.rclone` through an autouse
`_inject_cloud_rclone(cloud_rclone)` method. Two files stay outside that
client fixture: `test_s3.py` needs neither fixture, because it builds
`S3Credentials`/`S3Client` directly and never constructs an `Rclone`; and
`test_rclone_config.py` requests `do_spaces_config` directly, because it
only parses the config text. `tests/cloud/test_conftest.py` covers the
fixture's own skip and config-building logic offline with monkeypatched
env vars, so it needs no `cloud` marker.

Do not commit or push unless explicitly asked. Keep one logical change per
commit and follow the authorship and commit-message rules in `code_style.md`.

## Guide for common changes

### Change a public operation

1. Capture current RC call parameters, return values, and failure behavior
   with a unit test using a fake RC client.
2. Put new RC-call construction in the relevant `operations/*_embedded.py`
   module.
3. Expose the operation through the curated `Rclone` client.
4. Test empty inputs, explicit `False` options, caller-owned arguments,
   and RC-call failure (`RcCallError`).

### Change native build/toolchain handling

Keep `native/toolchain.toml` as the single source of truth for the pinned Go
version, C compiler toolchains, and the `native/rclone` fork URL and branch
names; the commit itself is the submodule gitlink.
`scripts/native/build.py` validates the resolved Go version and compiler
paths against it and fails loudly on a mismatch, rather than silently
building with the wrong toolchain.

Any change here needs a canonical wheel build (and `tests/native`) on every
affected platform - there is no fake-toolchain unit-test substitute for a
real native build.

### Bump rclone or add a platform

1. Move the `native/rclone` submodule pin to the new upstream commit on the
   `rclone-kit/integration-v1` branch (rebasing rclone-kit's own patches as
   needed), update `native/toolchain.toml`'s `rclone_upstream_version`, and
   push the branch to the fork remote - `scripts/native/verify_submodule_pin.py`
   (and CI) checks the pin is actually fetchable from there.
2. For a new platform, add its `NativeTarget` to
   `runtime/native_platform.py`'s registry (wheel platform tag,
   library/executable filenames) and its `OperatingSystem`/
   `MachineArchitecture` mapping in `runtime/platform.py` if new.
3. Ensure `_build_backend.py` emits the intended exact wheel tag.
4. Add a `tests/native` run and wheel job in CI for the new target.
5. Build, verify, smoke-test, and assemble the complete release set.
6. Update supported-platform documentation and the release record.

Adding a target is not complete when only the native build works;
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

What remains open: typing and linting depth, release publication
refinement, build isolation, source distributions, retry-aware
`sync`/`move`, and the domain-layer freeze. Improve them incrementally.
Everything the "Implementation overview" above describes is current
behavior, not a plan.

Operations rclone exposes over RC that the client does not surface yet:

- `hashsum()` (`operations/hashsum`) - `FileItem.hash` exists and is never
  populated;
- `mkdir()`/`rmdir()` (`operations/mkdir`, `operations/rmdir`) -
  `RemoteFS.mkdir` only warns that it is not supported;
- `about()` (`operations/about`), for quota and free space;
- runtime bandwidth limiting (`core/bwlimit`).

29 test files still use `unittest.TestCase` instead of the pytest style
`docs/code_style.md` prescribes: 22 under `tests/cloud`, 6 under
`tests/unit`, and 1 at the `tests/` root.

Two structural changes to `client.py` were evaluated against its measured
shape and rejected; the measurement is what makes them re-decidable rather
than re-arguable. The class is 69 methods, of which 49 have a single
statement under their docstring; 11 of the remaining 20 are lifecycle
plumbing (`__init__`, `close`, `_close_tracked_resources`,
`_embedded_config_path`, three `_ensure_*` accessors, four `_track_*`
helpers), leaving 9 methods that hold any real logic. Its size is 294
declared parameters and 353 docstring lines of API surface wrapped around
almost no logic, which is a facade, not a tangle. Mirroring the surface
under `rclone.s3`/`rclone.mounts`/`rclone.serve`/`rclone.config` namespaces
cannot work, because `rclone.config` is already a documented public
`Config` attribute. Splitting the methods across mixins would give each
mixin `self._rc_client`, `self._client_id`, `self.config` and
`self._ensure_job_monitor()` without owning any of them, so type-checking
them needs a shared Protocol every mixin inherits - six files plus a
protocol in place of one greppable file, moving lines while removing none.
Logic that was not facade lives in `operations/` instead: the copy/sync/move
RC-method constants beside `start_directory_transfer_embedded`, and
`save_to_db`'s optional-dependency guard and paging loop in
`operations/db_ops.py`, which must sit outside `rclone_kit.db` because
importing that package is exactly what fails without the `database` extra.

Typing and linting made a first pass, not the full rollout. Pyright
`strict` covers `settings.py` and `group_files.py` (both genuinely
0-error, not just near-zero); every other module trialed
returns only `reportMissingTypeStubs` cross-module noise from importing a
non-strict sibling, so broadening the list should wait on real `ANN`
progress rather than adding more noisy modules. Current ignore-family
counts over `src`, `tests`, and `scripts` together: `S101` 1427, `TRY` 244
(`TRY003` 191 of it), `ANN` 211 (`ANN001` 129), `FBT001`/`FBT002`/`FBT003`
96/51/28, `PLR0913` 52, `PTH` 28, `A001`/`A002` 13/10. These move with
every commit - re-run `uv run ruff check --select <CODE> --no-cache
--statistics .` before trusting them. `T201` is not among them: it is
enforced rather than ignored, as "Diagnostics and the error taxonomy"
describes.

Aim the `ANN` work at `s3/multipart/` (39 findings) and `fs/` (33 -
`fs/filesystem.py` 16, `fs/walk_threaded*` 15), which together are most of
the remaining surface and also where the remaining concurrency hazards
live. The domain layer accounts for 3 findings, so the domain-layer freeze
below is worth doing for its own reasons, not as a typing unblock.

The `@unittest.skip`'d tests in `tests/cloud` are not a backlog of missing
fakes. Each one either needs a real OS mount facility (FUSE/WinFsp) or
exercises byte-for-byte transfer, range-download, or multi-part-upload
correctness against live provider behavior that a hand-rolled fake would
not meaningfully validate - unlike the S3 multipart fakes, which stand in
for a well-defined boto3 method surface rather than for an entire storage
backend's actual bytes. Every skip reason states why that test is
disabled; one of them records a still-open bug, `RemoteFS.exists()` in
`test_fs_remote.py` reporting a file present immediately after a
successful `remove()`. `exists()` is a direct `operations/stat` call with
no HTTP serve in the path, so the cause is on the rclone or provider side -
most likely backend listing consistency after a delete - and it cannot be
root-caused without live bucket access.

| Area | Current constraint | Preferred next step | Required evidence |
|---|---|---|---|
| Typing and linting | `S101`, `ANN`, `TRY`, `FBT001`/`FBT002`/`FBT003`, `A001`/`A002`, `PLR0913`, `PTH`, `PLR0911`/`PLR0912`/`PLR0915` remain globally ignored. Pyright `strict` covers `settings.py` and `group_files.py`; every other candidate trialed (`access.py`, `chunk_store.py`, `config_discovery.py`) returns only `reportMissingTypeStubs` cross-module noise from importing a non-strict sibling, so broadening the list further should wait on real `ANN` progress rather than adding more noisy modules. | Make progress on the `ANN` family (even partial) before adding more files to `strict = [...]`; re-trial candidates afterward, since most of the remaining ones are only "near-zero" because of the cross-module noise, not their own quality. | Quality gates pass with a smaller ignore surface and no broad `Any` escape hatches in changed code. |
| Release publication | Done: `.github/workflows/release.yaml` builds, verifies, and publishes both certified wheels to PyPI via trusted publishing (OIDC, no stored token) on `v*` tags, gated by the `pypi-release` GitHub Environment. See `docs/release_process.md`. Artifact attestations (`actions/attest-build-provenance` or equivalent) are not yet added. | Add build provenance attestations to the `publish` job's uploaded wheels if supply-chain verification beyond trusted publishing becomes a requirement. | A published wheel carries a verifiable attestation, not just a trusted-publishing OIDC trail. |
| Domain-layer freeze | `RPath` carries a mutable `rclone` back-reference wired in after construction by `set_rclone`, which is why it cannot be a frozen dataclass and why `Dir.__init__`/`Dir.ls`/`Dir.walk` each guard it with `assert self.path.rclone is not None` - a nullable-then-asserted invariant the type system cannot help with. Removing the back-reference means rewriting every construction site and moving the convenience methods off the domain values, which is simply not done yet. Meanwhile `RPath` is hashable *and* fully mutable, so mutating any of the seven fields `_value()` reads after inserting one into a set or dict silently corrupts the container; `operations/listing_ops_embedded.py` already mutates `d.path.path` in place. | Make `RPath` frozen with no client reference and move `Dir.ls`/`Dir.walk`/`File.read_text` onto the client, which already has `rclone.ls(dir)`. Every `RPath` construction site that calls `set_rclone` needs rewriting - currently seven, in six functions: `Dir.__init__` and `Dir.__truediv__` (`dir.py`), `_stat_item` and the listing constructor in `fetch_ls_embedded` (`operations/listing_ops_embedded.py`), `_to_walk_dir` (`operations/traversal_ops.py`), and both branches of `to_path` (`util.py`). Note the cheap half is independent and need not wait: replacing `dir.py`'s three `assert self.path.rclone is not None` guards with a raised error restores an invariant that currently vanishes under `python -O`, since `S101` is globally ignored. | The `assert`-as-invariant pattern is gone from `dir.py`, `RPath` is frozen, and `Dir.ls`/`Dir.walk`/`File.read_text` live on the client. |
| Retry-aware `sync`/`move` | `sync()`/`move()` call upstream `sync/sync`/`sync/move`, which run once with no command-level retry loop, so their `OperationResult.attempts` is always empty and they expose no `retries` parameter - unlike `copy()`, which uses the fork's `rclonekit/copy`. The loop is deliberately not reimplemented in Python: rclone resets its accounting group's error state between attempts, which an out-of-process caller cannot do, so a naive retry would double-count stats and misattribute an abandoned attempt's errors. | Add `rclonekit/sync` and `rclonekit/move` to the fork alongside `rclonekit/copy`, reusing its `runWithRetries`. Needs a native rebuild and a submodule pin move, so it cannot ride along with a Python-only change. | `sync()`/`move()` report populated `attempts` and honour `retries`/`retries_sleep`, with the same job-level tests `copy()` has. |
| Build isolation | Smoke tests poison proxies but do not enforce network denial. | Run them in a network-disabled container or namespace where supported. | A deliberate network attempt fails while the bundled native library still initializes and reports `BuildInfo`. |
| Source distributions | An sdist cannot yet build a complete certified wheel. | Keep wheel-only releases, or add a verified artifact input/download hook and test sdist-to-wheel builds on every target. | A built-from-sdist wheel passes the same verifier and smoke test. |
| `scan_missing_folders()` on a wholly missing dst root | On a backend with real directories (Drive, SFTP, local, ...), a `dst` root that does not exist makes the underlying `ls()` fail in the background walk thread. That failure is collected there and re-raised by the generator to its caller after teardown, so the caller gets the real error - not an empty result, and not a silently truncated scan. Whether "the whole dst is missing" deserves a report rather than an error is still open. | Optional, only if callers turn out to want it: report a wholly missing dst root as "every src directory is missing" instead of raising the listing error. | `tests/unit/test_scan_missing_folders.py` proves a background walk failure reaches the caller, and that a consumer's `KeyboardInterrupt` propagates while the worker is still joined. |

Keep improvement pull requests small. Establish the contract with tests,
change one boundary, and remove the old path in the same change once its
in-tree callers have migrated.

## Pull request checklist

- [ ] The change is one coherent behavior or refactoring.
- [ ] Public API changes are intentional, with their tests and documentation
      updated alongside them.
- [ ] Unit tests cover success, failure, cleanup, and platform edge cases.
- [ ] Optional features still import without their extras installed.
- [ ] Diagnostics and log records never carry credentials or config secrets.
- [ ] Formatting, Ruff, Pyright, and unit tests pass; `tests/native` passes
      too when a native build is available locally.
- [ ] A canonical wheel build passes when distribution behavior changed.
- [ ] Documentation and authoritative constants agree.
- [ ] No generated executable, secret, cache, or build artifact is tracked.
