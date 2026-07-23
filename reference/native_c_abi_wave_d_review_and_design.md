# Native C ABI migration review and Wave D implementation design

Status: implementation review and proposed follow-up design

Date: 2026-07-23

Reviewed branch: `native-c-abi-migration` at `1d2f63629e2a91f5c3dc200d46c2fe285cf48413`

Compared with: `main` at `d46a443a661ade015bbca8027ab9ad9418536132`

Pinned native source: `native/rclone` at `285c4e403` on `rclone-kit/integration-v1`

Related documents:

- [C ABI implementation plan](rclone_c_abi_implementation_plan.md)
- [CLI-to-C-ABI migration plan and ledger](rclone_cli_to_c_abi_migration_plan.md)
- [C-shared library investigation](rclone_c_shared_library_investigation.md)
- [remote OAuth upstream change](rclone_remote_oauth_upstream_change.md)

## 1. Purpose and review outcome

This document reviews all work currently present on `native-c-abi-migration`, then defines the
implementation and software architecture for the rest of Wave D. It is intentionally a work plan,
not an implementation. No production code is changed by this review.

The branch has a sound foundation:

- the fork is pinned as the production native source;
- the rclone-kit-owned C ABI is small and versioned;
- raw C pointers stop at the `ctypes` binding;
- `RcloneRuntime`, `RcClient`, and operation adapters form useful boundaries;
- embedded execution is explicit and never silently falls back to `rclone.exe`;
- the completed Wave A-C operations and the first Wave D slice have unit and native tests; and
- current executable code passes the available checks.

Wave D should not continue as a mechanical `sync/copy` call. The pinned rclone implementation makes
four concerns inseparable from the transfer port:

1. `sync/copy` does not provide the command-level retry loop used by `rclone copy`.
2. `_async: true` returns an rclone job whose terminal record expires after about 60 seconds by
   default.
3. `job/status` does not contain live transfer progress; progress belongs to the job's accounting
   group and is read through `core/stats`.
4. the current native finalizer does not stop unfinished asynchronous jobs.

The recommended solution is therefore:

- preserve the existing blocking `Rclone.copy*()` behavior during the compatibility release;
- introduce a public `JobHandle` through an explicit `Rclone.start_copy()` method;
- introduce a public, subprocess-independent `OperationResult`;
- use one lazily started job monitor per embedded `Rclone` client so terminal state is captured before
  rclone expires it;
- add a small downstream-only `rclonekit/copy` RC endpoint that preserves rclone command retry
  semantics while still running as one asynchronous RC job;
- make job ownership and cancellation part of `Rclone.close()` before finalizing the runtime; and
- wrap `OperationResult` in the existing `CompletedProcess` only at the compatibility boundary.

This design deliberately avoids a return type that changes according to execution mode, a
`wait=False` union return, one Python thread per job, parsing arbitrary CLI arguments, and Go-to-Python
callbacks.

## 2. Review scope and evidence

### 2.1 Branch state

At review time the branch is 14 commits ahead of `main`. The working tree was clean before this
document was created. The implementation includes:

- the native submodule and reproducible build/manifest/smoke tooling;
- the downstream `librclone/rclonekit` C ABI bridge;
- Python `ctypes`, native library discovery, runtime, RC client, and path layers;
- opt-in `execution="embedded"` construction;
- embedded implementations for the completed Wave A-C ledger rows; and
- the first Wave D slice: T01 `cleanup`, T02 `copy_to`, T07 `purge`, with successful T11/T12 reads
  demonstrated transitively.

The three pending methods are still CLI-only:

| Ledger row | Public method | Current implementation | Required embedded operation |
| --- | --- | --- | --- |
| T03 | `copy()` | `copy_tree()` builds `rclone copy` arguments | retry-aware async directory copy |
| T04 | `copy_dir()` | `copy_directory()` builds `rclone copy` arguments | same engine, legacy option policy |
| T05 | `copy_remote()` | `copy_between_remotes()` builds `rclone copy` arguments | same engine with remote roots |

### 2.2 Validation performed

The following checks were run against the reviewed branch:

| Check | Result |
| --- | --- |
| `uv run pytest tests\unit -q` | 523 passed, 1 platform skip |
| `uv run pytest tests\native -q` | 34 passed |
| `uv run ruff check .` | passed |
| `uv run pyright src tests scripts _build_backend.py` | 0 errors, 0 warnings |
| `go test ./fs/rc/jobs ./fs/sync ./librclone/rclonekit/bridge` | passed |
| `git diff --check main...native-c-abi-migration` | failed only on existing Markdown hard-break whitespace/EOF whitespace |

Running bare `uv run pyright` is not a useful repository gate at present because it also analyzes
generated `build/` copies and both complete rclone checkouts. The scoped command above is the grounded
source/test result. A later tooling cleanup may add explicit Pyright exclusions, but that is not a
Wave D production-code blocker.

### 2.3 Pinned rclone behavior verified from source

The design below is based on the pinned source, not on an assumed RC contract:

- `fs/sync/rc.go` registers `sync/copy`; it calls `sync.CopyDir` once.
- `cmd/copy/copy.go` runs the CLI operation through `cmd.Run(true, true, ...)`.
- `cmd.Run` owns the high-level retry loop, fatal/no-retry classification, error resets, and retry
  sleep. Calling `sync/copy` through RC bypasses that loop.
- `librclone.RPC` runs every RC invocation through `jobs.NewJob`.
- `_async: true` returns `{jobid, executeId}` and detaches the job context.
- an explicit `_group` becomes the accounting stats group; otherwise rclone uses `job/<id>`.
- `job/status` returns ID, execute ID, group, times, duration, error, finished, success, and output.
- despite the help text mentioning progress, the `Job` JSON structure has no progress field.
- `core/stats(group=...)` returns bytes, checks, transfers, totals, errors, fatal/retry flags, speed,
  ETA, active transfers, and active checks.
- finished job records expire after `rc_job_expire_duration`, which defaults to 60 seconds and is
  checked every 10 seconds.
- `job/stop` cancels the job context; terminal status subsequently reports `context canceled` for the
  current implementation.
- the upstream `librclone.Finalize()` contains an unresolved TODO about unfinished async jobs and only
  triggers garbage collection.

These facts are the reason Wave D needs an owned job lifecycle rather than only a new request mapper.

## 3. Review findings

Findings are ordered by implementation risk. “Required” means the issue must be addressed before T03-
T05 can be called complete.

### F1 — High: `_config.Retries` does not reproduce `rclone copy --retries`

Evidence:

- `copy_tree()` currently passes `--retries` to the CLI.
- the CLI enters `cmd.Run`, which performs the high-level attempts.
- the RC `sync/copy` handler calls `CopyDir` once and never enters `cmd.Run`.
- setting `Retries` in per-call `_config` changes the value visible in the context but does not create
  the absent retry loop.

Impact:

- a direct async `sync/copy` port would silently reduce the default `copy()` behavior from up to three
  high-level attempts to one;
- transient cloud failures would regress even though the request JSON appeared to preserve the
  option; and
- tests that only use local or memory backends would probably miss the regression.

Required change:

- implement the downstream `rclonekit/copy` RC endpoint specified in section 8; and
- add deterministic Go tests proving retry, no-retry, fatal, cancellation, and retry-sleep behavior.

Do not claim retry parity merely because `_config` contains `Retries`.

### F2 — High: native finalization does not own unfinished jobs

Evidence:

- `bridge.Finalize()` says it performs job/accounting cleanup but delegates to
  `librclone.Finalize()`;
- the delegated function explicitly asks what should happen to unfinished async jobs and performs no
  cancellation; and
- `RcloneRuntime.close()` currently ignores the status/output returned by the native `finalize()`
  call and marks itself closed.

Impact:

- after T03-T05, `Rclone.close()` could make the Python control surface unusable while Go transfer
  goroutines continue to access remotes and local files;
- resources could remain live until process exit; and
- cancellation/finalization failures would be hidden from the caller.

Required change:

- track every job created by a public embedded client;
- cancel and observe terminal state before finalizing an owned runtime;
- make the downstream finalizer independently stop any remaining running RC jobs as a safety net;
- check and raise on the result of native finalization; and
- leave the runtime open if safe shutdown cannot complete, rather than reporting a false close.

### F3 — High: an on-demand handle can lose terminal state to rclone job expiry

Evidence:

- rclone deletes completed job records after 60 seconds by default;
- a user may call `start_copy()`, do other work, and call `wait()` later; and
- no current Python component polls or caches terminal job status.

Impact:

- a completed transfer could become indistinguishable from an invalid job ID;
- its output and final error could be lost; and
- a public `JobHandle` would not meet its basic durability contract.

Required change:

- add one lazy monitor per embedded client;
- poll and cache terminal status and final stats while jobs are owned; and
- distinguish “job expired/lost before observation” from an ordinary operation failure.

Increasing rclone's global expiry duration is not sufficient. It only moves the race and retains more
global state.

### F4 — High: T11/T12 success is transitive, but their failure contract is not

Evidence:

- `read_bytes()` and `write_bytes()` catch `subprocess.CalledProcessError` and translate it to
  `RcloneCommandError`;
- embedded `copy_to(check=True)` raises `RcCallError` instead; and
- the native transitive test verifies successful reads but not missing-source, authorization, network,
  partial-output, or cleanup failures.

Impact:

- the exception type depends on execution mode;
- callers following current documentation may fail to catch embedded errors; and
- the ledger currently overstates T11/T12 completion.

Required change:

- introduce the execution-independent operation error hierarchy in section 6;
- update T02, T09, T11, and T12 call paths to use it; and
- test the negative and temporary-file cleanup cases before marking T11/T12 complete.

### F5 — Medium: completed T01/T02/T07 calls still monopolize the runtime

`cleanup_embedded`, `copy_file_to_embedded`, and `purge_dir_embedded` use synchronous RC calls. The
runtime lock is therefore held for the whole operation. A large single-file copy, slow cleanup, or
large purge cannot report progress or be cancelled, and blocks unrelated RC calls on the same
runtime.

The first Wave D implementation was reasonable as a vertical slice, but the final Wave D architecture
should route all potentially long mutating operations through the same job machinery:

- T01 `cleanup`;
- T02 `copy_to`;
- T03-T05 copies; and
- T07 `purge`.

Their public methods can remain blocking by immediately waiting on the handle. “Asynchronous RC job”
does not require an asynchronous public method.

### F6 — Medium: result compatibility is described but not encoded as a boundary

The embedded transfer helpers currently fabricate a `subprocess.CompletedProcess` and place a human
description in `args`. This temporarily preserves `.ok` and `.returncode`, but it has no real command,
stdout, stderr, or subprocess.

If T03-T05 repeat this pattern, fake process details will spread into progress, retries, and partial
attempt reporting. Instead:

- all new transfer internals must produce `OperationResult`;
- only `CompletedProcess.from_operation_result()` may create the compatibility view; and
- CLI and embedded public `copy*()` methods must keep the same return type during a given release.

### F7 — Medium: job identity requires both `executeId` and `jobid`

Job IDs restart from one when rclone restarts. Rclone explicitly documents the pair of execute ID and
job ID as the unique identity. A parser that stores only `jobid` can attach a stale handle to an
unrelated operation after a worker restart.

Every handle and status parser must store and validate both values. A mismatch is a typed runtime-
identity error, never an ordinary operation failure.

### F8 — Medium: `RcPath.as_parent_and_name()` rejects a valid local basename

For a local relative path such as `file.txt`, `RcPath.parse()` produces no separator and
`as_parent_and_name()` raises `ValueError`. The CLI accepts this path and interprets its parent as the
current directory.

Before expanding T02 or using the path helper elsewhere, normalize a single local basename to
`fs="."`, `remote="file.txt"`. Add Windows, POSIX, Unicode, UNC, remote-root, and inline-remote cases.

### F9 — Medium: the migration gate is still Markdown-only

The migration plan calls for a machine-readable parity ledger before broadening the migration, but
`tests/parity/coverage.toml` (or an equivalent file) does not exist. The no-silent-fallback guard is
strong at the backend boundary, yet CI cannot currently prove that all ledger rows and platform gates
required for a default switch are complete.

Create the coverage file during the Wave D preparation commit and associate T01-T05/T07/T11/T12 with
their unit, native-local, memory, Windows, Linux, and live-provider tests.

### F10 — Low: implementation status comments and hygiene need a cleanup pass

- the `Rclone` constructor docstring still says only `obscure()` is embedded;
- `test_client_embedded.py` has the same early-wave description;
- the migration document's completed status for T11/T12 should be qualified until F4 is fixed; and
- `git diff --check` reports existing Markdown trailing whitespace and extra EOF blank lines.

These are not runtime blockers, but they should be fixed in a documentation-only commit so review
state agrees with the code.

## 4. Architectural decisions

### D1 — Preserve blocking methods; add an explicit start method

Existing callers expect `copy()`, `copy_dir()`, and `copy_remote()` to return only after the transfer
finishes. Preserve that behavior.

Add this embedded operation:

```python
handle = rclone.start_copy(
    src="source:bucket/prefix",
    dst="destination:bucket/prefix",
    transfers=32,
    checkers=1000,
    low_level_retries=10,
    retries=3,
)

while not handle.done:
    report(handle.stats())

result = handle.wait(timeout=600)
```

The blocking method becomes conceptually:

```python
def copy(...) -> CompletedProcess:
    if execution == "cli":
        return existing_cli_copy(...)
    result = start_copy(...).wait()
    return CompletedProcess.from_operation_result(result)
```

Do not add `wait: bool` to `copy()`. A return type of
`CompletedProcess | JobHandle` is difficult to type, document, and use correctly.

`start_copy()` is embedded-only during the opt-in transition and raises a clearly named execution-mode
error for CLI clients. It becomes the normal implementation once embedded execution is the default.
There is no temporary CLI-thread emulation and no hidden executable launch.

### D2 — Keep one rclone job per logical copy

The retry loop belongs inside the downstream retry-aware RC endpoint. The outer `_async` RC wrapper
therefore creates one job ID for the complete logical operation, including all high-level attempts.

This preserves the simple `JobHandle` contract and avoids a Python supervisor that must conditionally
start replacement jobs. Attempt details are returned in the terminal job output and become
`OperationAttempt` values.

Wave E partitioned copies will need an aggregate operation coordinator above multiple `JobHandle`
instances. That later composite type should not be smuggled into Wave D's atomic handle.

### D3 — Use one lazy monitor per embedded client

`_JobMonitor` is an internal service owned by one embedded `Rclone` instance:

- it is created only on the first `start_job`, never at import or client construction;
- it owns one daemon thread, regardless of the number of jobs;
- it polls all due job records through the existing serialized runtime;
- it stores the latest status and stats and wakes waiting handles with a `Condition`;
- it captures terminal status and final stats before rclone's expiry window;
- it deletes an operation's stats group after its final snapshot is cached; and
- it is stopped and joined by `Rclone.close()`.

No user callback runs on the monitor thread. Progress is pull-based through `handle.stats()`.

### D4 — Use unique, explicit accounting groups

Every started job receives an unguessable, non-secret group such as:

```text
rclone-kit/<client-uuid>/<operation-uuid>
```

The group is created before the start call and passed as `_group`. This gives progress a stable key
before the returned job ID is known, prevents unrelated transfers from contaminating stats, and makes
cleanup deterministic.

Never poll global stats for a handle.

### D5 — Keep the public model independent of RC JSON

Wire payloads are parsed at the RC boundary. Operation code and callers receive frozen typed values,
not arbitrary dictionaries. Unknown response fields may be retained in a read-only `details` mapping
for diagnostics, but must not replace the typed contract.

### D6 — Treat native shutdown as an ownership operation

Shutdown order is mandatory:

1. reject new jobs;
2. request cancellation for every active job owned by the client;
3. continue polling until each job is terminal or the shutdown deadline is reached;
4. cache final results and delete stats groups;
5. stop and join the monitor;
6. finalize the owned native runtime; and
7. mark the client closed.

If the deadline is reached, raise a typed shutdown error and keep the runtime usable so the caller may
retry cancellation or wait longer. An injected runtime is never finalized by the client, but the
client still cancels jobs that it started.

The downstream C finalizer also enumerates and stops any remaining running jobs before delegating to
upstream finalization. This is defense in depth for direct C callers and programming mistakes above
the ABI.

### D7 — Preserve method-specific historical defaults

Consolidating T03-T05 means sharing an engine, not silently making their historical tuning identical:

- `copy()` keeps its current tuned defaults: checkers 1000, transfers 32, low-level retries 10, and
  high-level attempts 3;
- `copy_dir()` keeps rclone's normal defaults unless typed options are added to that method; and
- `copy_remote()` keeps rclone's normal defaults unless typed options are added to that method.

The aliases may be deprecated later, but Wave D should not change their performance profile without a
separate public API decision and benchmarks.

## 5. Public result model

Add the following values in a focused top-level module, recommended as
`src/rclone_kit/operation.py`. These are domain objects, not native or RC implementation details.

### 5.1 `JobState`

```python
class JobState(StrEnum):
    RUNNING = "running"
    CANCELLATION_REQUESTED = "cancellation_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"
```

Rclone reports only `finished`, `success`, and an error string. The Python layer adds
`CANCELLATION_REQUESTED` from owned local state. `CANCELLED` is used only after this client requested
cancellation and terminal status is consistent with cancellation. Do not classify an unrelated
failure as cancellation merely because a cancel call raced with completion.

### 5.2 `JobStatus`

Recommended immutable fields:

```text
JobStatus
├── job_id: int
├── execute_id: str
├── group: str
├── state: JobState
├── started_at: datetime
├── ended_at: datetime | None
├── duration: float
├── error: str | None
└── output: Mapping[str, object]
```

All datetimes are timezone-aware. Parsing is strict for identity and terminal flags, tolerant for
forward-compatible extra fields, and tested against the exact pinned JSON.

### 5.3 `TransferStats`

Model at least the fields Wave D consumes:

```text
TransferStats
├── bytes: int
├── total_bytes: int
├── checks: int
├── total_checks: int
├── transfers: int
├── total_transfers: int
├── errors: int
├── fatal_error: bool
├── retry_error: bool
├── speed: float
├── eta_seconds: float | None
├── elapsed_seconds: float
├── active_transfers: tuple[ActiveTransfer, ...]
└── active_checks: tuple[str, ...]
```

Missing optional arrays become empty tuples. Numeric validation rejects booleans masquerading as
integers. Unknown fields are retained only in diagnostic details.

### 5.4 `OperationAttempt`

The downstream copy endpoint reports command-level attempts:

```text
OperationAttempt
├── number: int
├── started_at: datetime
├── ended_at: datetime
├── duration: float
├── ok: bool
├── error: str | None
├── fatal_error: bool
└── retry_error: bool
```

An attempt is not a subprocess and does not have PID, args, stdout, stderr, or return code.

### 5.5 `OperationResult`

```text
OperationResult
├── ok: bool
├── operation: str
├── source: str | None
├── destination: str | None
├── job_ids: tuple[int, ...]
├── stats: TransferStats | None
├── warnings: tuple[OperationWarning, ...]
├── attempts: tuple[OperationAttempt, ...]
├── started_at: datetime
├── ended_at: datetime
├── duration: float
├── cancelled: bool
└── error: str | None
```

For Wave D `job_ids` contains exactly one value. It is already a tuple because Wave E will aggregate
partition jobs without redesigning the result.

`ok` is explicit and validated against the terminal status. Do not infer success from an empty error
string alone.

### 5.6 `CompletedProcess` compatibility

During the compatibility release:

- `Rclone.copy*()` continues to return `CompletedProcess` in both modes;
- embedded methods construct it only through `CompletedProcess.from_operation_result()`;
- the wrapper exposes the underlying result through `operation_result`;
- `.ok` delegates to the result;
- `.returncode` is the documented synthetic `0` or `1`; and
- `.stdout`, `.stderr`, `.completed`, `failed()`, `successes()`, and command formatting remain
  deprecated/CLI-only and must not be populated with invented process data.

At the embedded-first major release, blocking operation methods return `OperationResult` directly.
Do not make this switch inside Wave D unless the release itself is intentionally a breaking major.

## 6. Error model and `check` semantics

Add execution-independent operation errors under `RcloneKitError`:

```text
OperationError
├── OperationStartError
├── OperationFailedError
├── OperationCancelledError
├── OperationTimeoutError
├── JobExpiredError
├── JobIdentityError
└── OperationShutdownError
```

Rules:

- malformed input and unsupported typed options fail before starting a job;
- an RC failure while creating the job raises `OperationStartError` with the original `RcCallError` as
  its cause;
- terminal failure with `check=True` raises `OperationFailedError` carrying the complete
  `OperationResult`;
- terminal failure with `check=False` returns `OperationResult(ok=False)`;
- a confirmed cancellation with `check=True` raises `OperationCancelledError`, also carrying the
  result;
- `wait(timeout=...)` raises `OperationTimeoutError` without automatically cancelling the operation;
- a timeout applies to observation, not to the duration of one already-dispatched C call;
- losing an unobserved job to rclone expiry raises `JobExpiredError`, never a generic
  `RcCallError("job not found")`; and
- execute-ID mismatch raises `JobIdentityError` immediately.

The compatibility `copy()` method applies its existing `check` default through `get_check()`. The new
`start_copy()` stores that policy on the handle so `wait()` behaves consistently.

`copy_dir()` and `copy_remote()` currently do not expose `check`; preserve their non-raising result
behavior during the compatibility release.

## 7. `JobHandle` contract

Recommended public surface:

```python
class JobHandle:
    @property
    def job_id(self) -> int: ...

    @property
    def execute_id(self) -> str: ...

    @property
    def group(self) -> str: ...

    @property
    def done(self) -> bool: ...

    def status(self) -> JobStatus: ...
    def stats(self) -> TransferStats: ...
    def wait(self, timeout: float | None = None) -> OperationResult: ...
    def cancel(self) -> bool: ...
    def close(self) -> None: ...
```

It is also a context manager.

### 7.1 Status

`status()` returns the monitor's latest immutable snapshot. It may request an immediate refresh when
the cached value is older than the configured poll interval, but callers do not directly manipulate
RC dictionaries.

### 7.2 Stats

`stats()` reads the explicit group. After completion it returns the cached final snapshot, even after
the native stats group has been deleted.

### 7.3 Wait

`wait()` uses a monotonic deadline and a condition variable; it does not busy-loop or sleep while
holding the runtime lock. Multiple threads may wait on the same handle. All receive the same cached
result or equivalent typed exception.

### 7.4 Cancel

`cancel()` is idempotent:

- it returns `False` if terminal state was already observed;
- otherwise it records cancellation intent, calls `job/stop`, schedules an immediate status poll, and
  returns `True` when the request was accepted;
- it does not claim the operation is terminal;
- it does not wait; callers use `wait()` when they need confirmed termination; and
- a stop/status race is resolved from terminal job state, never from timing alone.

### 7.5 Close and context management

`close()` cancels an unfinished owned job and waits for the bounded handle shutdown interval. It is
idempotent. Exiting a `with JobHandle` block calls `close()`; it does not cancel a job that is already
terminal.

Do not implement `__del__` with RC calls. Python finalization order and native-library lifetime make
network/FFI work from a destructor unsafe.

## 8. Downstream retry-aware copy endpoint

### 8.1 Why a downstream endpoint is preferred

There are three possible places for high-level retries:

| Location | Result |
| --- | --- |
| pretend `_config.Retries` is enough | incorrect; no retry loop executes |
| Python starts another `sync/copy` job after failure | multiple job identities, expiry races, duplicated retry classification, more monitor complexity |
| downstream RC function loops around `sync.CopyDir` | one job, native error classification, context cancellation, closest CLI parity |

Use the third option. The project already owns a custom rclone fork and a downstream C bridge. A small,
isolated endpoint is lower long-term risk than reimplementing rclone's error taxonomy in Python.

### 8.2 Source layout

Recommended fork paths:

```text
librclone/rclonekit/
├── bridge/
│   ├── bridge.go
│   ├── bridge_test.go
│   └── imports.go
└── rc/
    ├── copy.go
    └── copy_test.go
```

`bridge/imports.go` blank-imports the downstream RC package so its `init()` registers the method. Do
not modify the behavior of upstream `sync/copy`; that would broaden the fork patch and alter existing
RC callers.

### 8.3 RC contract

Register `rclonekit/copy` with these operation parameters:

- `srcFs`: full source filesystem/path, string or RC filesystem config object;
- `dstFs`: full destination filesystem/path, string or RC filesystem config object; and
- `createEmptySrcDirs`: optional boolean.

The normal RC wrapper consumes `_async`, `_group`, `_config`, and `_filter` before invoking the
function. The endpoint reads typed values from `fs.GetConfig(ctx)` and the filter from the context.

### 8.4 Retry algorithm

The endpoint follows the relevant `cmd.Run` behavior without importing Cobra or `cmd`:

1. read the configured total attempt count (`Retries`);
2. run `sync.CopyDir`;
3. apply `fs.CountError(ctx, err)` as the command runner does;
4. capture attempt duration, error, fatal flag, and retry flag;
5. return immediately on success;
6. stop on context cancellation, fatal error, or no-retry classification;
7. if another attempt remains, reset accounting errors for the group;
8. wait for `RetriesInterval` with a context-aware timer; and
9. run the next attempt.

The final output contains the attempt array on both success and failure. The function returns the
final native error as `err`, allowing the outer rclone job to set `success=false` and preserve the
error in `job/status`.

Use rclone's scoped `accounting.Stats(ctx)`, never `GlobalStats()`, because multiple library jobs may
run in the same process.

### 8.5 Go tests

Tests must prove:

- first-attempt success;
- retryable failure then success;
- all attempts exhausted;
- fatal error prevents retry;
- no-retry error prevents retry;
- configured interval is honored through an injectable clock/timer or a minimal bounded test;
- cancellation interrupts an active attempt;
- cancellation interrupts retry sleep;
- output contains every completed attempt; and
- two concurrent groups do not share accounting/error state.

### 8.6 Upstream policy

Keep this endpoint on `rclone-kit/integration-v1`. It is product-specific orchestration and is not part
of the narrow OAuth upstream proposal. If upstream later provides a retry-aware RC contract, replace
the downstream endpoint behind the Python protocol and remove it in a dedicated fork commit.

## 9. Options and filesystem encoding

### 9.1 Typed transfer options

Create a frozen `TransferOptions` value and a single encoder, recommended in
`operations/transfer_options.py`.

Wave D fields:

- `checkers`;
- `transfers`;
- `low_level_retries`;
- `retries` (total high-level attempts, matching rclone's existing naming);
- `multi_thread_streams`; and
- `create_empty_src_dirs`.

Exact `_config` keys at the pinned commit are Go field names:

| Python option | `_config` key |
| --- | --- |
| `checkers` | `Checkers` |
| `transfers` | `Transfers` |
| `low_level_retries` | `LowLevelRetries` |
| `retries` | `Retries` |
| `multi_thread_streams` | `MultiThreadStreams` |

When multi-thread streams are explicitly supplied, also encode `MultiThreadSet=true`. The CLI sets
that companion flag when the command option changes, and rclone's multi-thread decision logic reads
it.

Validation occurs before RC dispatch. Values must be positive integers. Compatibility normalization
for the existing `copy()` signature must be tested separately because its current CLI code treats
some falsey values as defaults.

### 9.2 Arbitrary arguments

- `copy(other_args=...)`, `copy_dir(args=...)`, and `copy_remote(args=...)` continue working on CLI;
- any nonempty arbitrary argument collection raises `UnsupportedEmbeddedOptionError` in embedded
  mode; and
- values are not included in the error message because they may contain credentials.

Do not add a partial command-line parser.

### 9.3 S3 `no_check_bucket`

The current CLI methods always add `--s3-no-check-bucket`. This is a backend option, not an
`fs.ConfigInfo` field, so it cannot be preserved through `_config`.

Represent an S3 filesystem parameter using rclone's documented RC config-object form when the source
or destination refers to a configured S3 remote:

```json
{
  "_name": "remote-name",
  "_root": "bucket/prefix",
  "no_check_bucket": "true"
}
```

Introduce an internal `RcFsSpec` encoder rather than teaching every transfer method this format. It
must leave local paths and non-S3 remotes as strings. Add live S3 coverage to prove this behaves like
the current flag and does not leak credentials into payloads or logs.

Also apply the same decision to T02 `operations/copyfile`; the current embedded implementation does
not reproduce the CLI's S3 override.

### 9.4 Path normalization

`sync/copy` receives full `srcFs`/`dstFs` values, unlike `operations/copyfile`, which needs parent/name
splits. Keep these cases explicit in adapter names and tests. Do not call `as_parent_and_name()` for a
directory copy.

Expand `RcPath`/`RcFsSpec` tests before reuse:

- remote root and nested remote path;
- local absolute Windows and POSIX path;
- relative local basename and nested relative path;
- UNC path;
- Unicode;
- trailing slash;
- inline remotes; and
- a literal backslash inside a remote object name.

## 10. Python source architecture

Recommended final Wave D layout:

```text
src/rclone_kit/
├── client.py
├── completed_process.py
├── exceptions.py
├── job.py                         # public JobHandle, internal monitor records
├── operation.py                   # public immutable result/status/stat types
├── native/
│   └── runtime.py                 # serialized ABI calls and checked finalization
├── rc/
│   ├── client.py                  # call() and start_job()
│   ├── errors.py
│   ├── jobs.py                    # RC job/status/stats/stop wire adapter
│   └── paths.py
└── operations/
    ├── transfer_options.py        # typed options and _config encoder
    ├── transfer_ops.py            # legacy CLI adapters
    └── transfer_ops_embedded.py   # operation-oriented embedded adapters
```

### 10.1 Dependency direction

```text
Rclone public facade
        |
        v
embedded transfer adapter ---> TransferOptions / RcFsSpec
        |
        v
JobHandle / _JobMonitor ---> RC job adapter ---> RcClient
        |                                      |
        v                                      v
OperationResult                          RcloneRuntime
                                               |
                                               v
                                         ctypes binding
```

Rules:

- `operation.py` imports no native, RC, client, or subprocess modules;
- `job.py` depends on a narrow RC-job protocol, not on `ctypes`;
- transfer adapters depend on a `JobStarter` protocol so fakes can test request mapping;
- only `native/abi.py` handles pointers;
- only `completed_process.py` knows both the legacy process wrapper and `OperationResult`; and
- `client.py` coordinates ownership but does not parse job JSON.

### 10.2 RC job adapter

Add a narrow operation-oriented interface:

```python
class RcJobClient(Protocol):
    def start(self, method: str, params: Mapping[str, object], group: str) -> RcJobRef: ...
    def status(self, ref: RcJobRef) -> JobStatus: ...
    def stats(self, group: str) -> TransferStats: ...
    def stop(self, ref: RcJobRef) -> None: ...
    def delete_stats(self, group: str) -> None: ...
```

`RcJobRef` contains `job_id`, `execute_id`, and `group`. It is internal. The adapter always injects
`_async=true` and `_group`; operation modules do not repeat those magic keys.

### 10.3 Monitor records

Each internal record stores:

- immutable job reference and operation metadata;
- latest typed status and stats;
- terminal result or terminal exception;
- cancellation intent and close state;
- next poll deadline/backoff; and
- one condition variable shared by waiters.

The monitor owns mutation. `JobHandle` is a thin thread-safe view over the record.

## 11. Public facade mapping for T03-T05

### 11.1 `start_copy()`

Add one canonical start method accepting `Dir | Remote | str` for both sides and the typed options
already present on `copy()`. It converts domain values to strings/specs, builds the transfer options,
starts `rclonekit/copy`, registers the job, and returns `JobHandle`.

### 11.2 `copy()`

- CLI path remains unchanged during compatibility.
- Embedded path calls `start_copy(...).wait()`.
- The completed result is wrapped once as `CompletedProcess`.
- `check` controls terminal failure behavior, not RC input.

### 11.3 `copy_dir()`

- Retain the public signature for this release.
- Reject nonempty `args` in embedded mode.
- Call the shared embedded engine with legacy/default rclone tuning and non-raising result behavior.
- Do not call `self.copy()` if doing so would accidentally apply `copy()`'s aggressive defaults.

### 11.4 `copy_remote()`

- Convert both `Remote` values to full roots such as `source:` and `destination:`.
- Apply the same arbitrary-argument and result policy as `copy_dir()`.
- Use the shared engine without aggressive `copy()` defaults.

### 11.5 T01/T02/T07 retrofit

After T03-T05 work end to end, migrate cleanup, file copy, and purge to the same job starter and result
boundary. Keep their public behavior stable, but remove synchronous long-running RC calls and
synthetic subprocess construction from their operation modules.

## 12. Lifecycle and finalizer implementation

### 12.1 Python client close

Extend `Rclone` with explicit closed/closing state and a lazily populated monitor. Public operations
raise `RuntimeClosedError` after close. `close()` remains idempotent.

Do not register a new atexit callback at import. If an atexit safety hook is required, register it
lazily when the first embedded runtime/job is created, following the repository's existing lazy-
registration tests.

### 12.2 Runtime close

Update `RcloneRuntime.close()` to:

- refuse new calls once finalization begins;
- inspect native finalize status/output through `_raise_for_lifecycle_status`;
- set `_closed=True` only after successful finalization; and
- remain idempotent after success.

The native ABI exists only on this feature branch relative to the reviewed `main`, so add
`RCLONEKIT_ERR_BUSY = -5` and `NativeBusyError` to the v1 contract now, before its first merge/release.
The binding must recognize the new status and leave the runtime open. If maintainers know that a v1
artifact was distributed independently from this branch, that fact overrides the repository evidence
and requires an ABI v2 bump instead of changing the shipped v1 contract.

### 12.3 Downstream finalizer safety net

Before calling upstream `librclone.Finalize()`, the bridge may invoke the registered `job/list`,
`job/stop`, and `job/status` functions directly through `rc.Calls.Get(...).Fn`. Calling the handlers
directly avoids creating additional wrapper jobs while enumerating jobs.

Use a bounded wait and return a typed native error if jobs cannot become terminal. Never report
successful finalization while transfers remain active.

## 13. Implementation sequence

Each phase below should be one or more focused commits. Do not combine native fork changes, Python
public API changes, and unrelated documentation cleanup in a single commit.

### Phase D0 — Establish gates and correct status

1. Add `tests/parity/coverage.toml` with every ledger row and current platform/test status.
2. Mark T11/T12 as success-path-complete but failure-contract-incomplete.
3. Add a test that patches process creation and executes every currently supported embedded method,
   proving no `rclone.exe` fallback occurs during operations.
4. Correct stale embedded-coverage docstrings.
5. Clean existing Markdown whitespace in a documentation-only change.

Exit gate: CI can identify Wave D rows and executable fallback automatically.

### Phase D1 — Define public values and errors

1. Add `operation.py` frozen dataclasses/enums.
2. Add operation errors and result-carrying exceptions.
3. Export stable public types from `rclone_kit.__init__`.
4. Add parser-independent unit tests for invariants, immutability, datetime handling, and result
   consistency.
5. Add `CompletedProcess.from_operation_result()` and compatibility tests without changing public
   method return types.

Exit gate: no operation module needs to manufacture a subprocess to report an embedded result.

### Phase D2 — Implement and test the low-level RC job boundary

1. Add `RcJobRef` and strict start-response parsing.
2. Add adapters for `job/status`, `core/stats`, `job/stop`, and `core/stats-delete`.
3. Validate `executeId` on every status.
4. Map “job not found” to an explicit expiry/lost condition only after checking cached state.
5. Test malformed JSON shapes, missing fields, booleans-as-integers, start failure, status failure, and
   stop races with fake `RcClient` responses.

Exit gate: one atomic native job can be started, polled, stopped, and represented without transfer
code.

### Phase D3 — Implement `JobHandle` and `_JobMonitor`

1. Add a fake clock and fake RC-job client test harness.
2. Implement lazy monitor start and one-thread scheduling.
3. Implement status/stats caching, condition-based waits, monotonic timeouts, and idempotent cancel.
4. Implement terminal snapshot and stats-group deletion.
5. Implement client shutdown ordering and injected-runtime ownership.
6. Add stress tests with many fake jobs to prove one monitor thread, bounded polling, no deadlocks, and
   correct wake-up of multiple waiters.

Exit gate: a delayed caller cannot lose a terminal result to the simulated expiry window.

### Phase D4 — Add the downstream retry-aware endpoint

1. Create `librclone/rclonekit/rc/copy.go` and tests.
2. Register it only in the downstream bridge imports.
3. Implement scoped retry classification and context-aware retry sleep.
4. Add attempt output on success and failure.
5. Add finalizer running-job cancellation and tests.
6. Move the submodule pin only after the fork commit is pushed and fetchable.
7. Build both native executable and shared library from the same new pin.

Exit gate: Go tests prove command-level retry semantics without importing CLI command packages.

Status: items 1-4 and 7 are done. `librclone/rclonekit/rc/copy.go` registers `rclonekit/copy`,
reusing `sync.CopyDir` inside a retry loop that mirrors `cmd.Run` (`cmd/cmd.go`) but reads/writes
`accounting.Stats(ctx)` (the calling job's own group) instead of `accounting.GlobalStats()`, so
concurrent library jobs never share error state. `copy_test.go` covers all ten cases from section
8.5 against an injectable `f func(context.Context) error` rather than real backends - first-attempt
success, retryable-then-success, all attempts exhausted, fatal error stops retrying, no-retry error
stops retrying, the configured `RetriesInterval` is honored (a short real sleep, not a simulated
clock), cancellation during an attempt, cancellation during the retry sleep, every attempt appears
in the output on both success and failure, and two concurrent groups don't share error state - all
passing under `go test` and `go test -race`. Verified against a full local rebuild
(`uv run python scripts/native/build.py --target windows-amd64`, which runs the whole Go test suite
as part of the build) and an ad hoc RC probe against the freshly built DLL: a real
`rclonekit/copy` call for a normal copy (one attempt, `success: true`) and for a missing source with
`_config: {Retries: 2, RetriesInterval: "10ms"}` (two recorded attempts, `success: false`,
`error: "directory not found"`).

Committed locally in the `native/rclone` submodule (`rclone-kit/integration-v1`, commit
`d498adafa`, message `librclone/rclonekit: add retry-aware asynchronous copy operation`) but
**not pushed** to `github.com/Johnz86/rclone.git`, and the parent repository's submodule pin is
**not moved** - per this document's own rule (item 6) and the migration invariant against
referencing an unfetchable commit, moving the pin requires the fork commit to be pushed and
fetchable first, which is a separate, explicitly-authorized action. `build/native/windows-amd64`
(gitignored) has been rebuilt locally from the current uncommitted submodule checkout so the
existing native test suite already exercises the new endpoint; this is safe to redo and does not
require the pin to move.

Item 5 (finalizer running-job cancellation and its tests, downstream in the bridge's `Finalize()`)
is not started - it belongs with the rest of Phase D3/D6's lifecycle work in section 12.3, not with
the copy endpoint itself, and doesn't block Phase D5/D6's Python-side work.

### Phase D5 — Add transfer option and filesystem-spec encoders

1. Implement `TransferOptions` and exact `_config` encoding.
2. Encode `MultiThreadSet` with explicit stream count.
3. Implement `RcFsSpec` including the S3 override.
4. Fix relative local basenames in `RcPath`.
5. Validate encoders against `options/local` or a purpose-built native test at the pinned commit.
6. Add all path cases from section 9.4.

Exit gate: request JSON is fully typed and parity differences are explicit.

### Phase D6 — Port T03-T05

1. Add `start_copy()` and its facade tests.
2. Port embedded `copy()` through the new endpoint and job handle.
3. Port `copy_dir()` with its legacy default/check policy.
4. Port `copy_remote()` with full remote roots.
5. Keep CLI command-vector tests unchanged.
6. Add native local and memory parity for success, skip-existing behavior, nested paths, empty
   directories, failure, timeout, cancel, and progress.

Exit gate: T03-T05 work under embedded execution without `rclone.exe`, with retry and cancellation
tests.

Status: done. `start_copy()` is embedded-only (raises `EmbeddedOnlyOperationError` under
`execution="cli"`) and returns a `JobHandle`; `copy()`, `copy_dir()`, and `copy_remote()` all share
it, differing only in their historical default/check policy per section 11.2-11.4 (`copy()` applies
its tuned profile and `check`-driven raise behavior; `copy_dir()`/`copy_remote()` use rclone's own
defaults and never raise). `Rclone.close()` was extended to cancel and wait for every job a client
started before finalizing, per section 12.1.

Native parity (`tests/native/test_start_copy_integration.py`) covers: a nested multi-directory tree
matching CLI byte-for-byte, an empty directory (zero transfers), a second pass over already-synced
files (zero transfers), a missing source both with `check=False` (non-raising) and `check=True`
(`OperationFailedError`), `wait(timeout=...)` racing the monitor's poll interval deterministically
(a timeout far below the default poll interval reliably fires before the first poll, rather than
depending on how fast the underlying copy happens to run), `cancel()` against a real in-flight job
(loosely asserted - a fast local copy commonly finishes before cancellation lands, so this only
proves the handle settles cleanly either way; real cancellation-interrupts-an-active-attempt
coverage belongs to the Go endpoint's own test suite, Phase D4), `copy_dir()`'s non-raising failure
and `args`-rejection, stats isolation between two concurrently-started jobs' accounting groups, and
the low-level `RcloneRcJobClient` used directly agreeing with the public facade. `copy_remote()`'s
native coverage is narrower than the others: `Remote` is a bare-root reference whose constructor
rejects a colon in the name, so it cannot address an arbitrary local test directory the way a
str/Dir target can; native coverage here is for the non-raising-failure and args-rejection paths,
with the Remote-to-string conversion and successful-dispatch path covered at the unit level instead
(`test_copy_remote_dispatches_to_start_copy` in `test_client_embedded.py`).

This phase required rebuilding the local native artifact (`build/native/windows-amd64`) after Phase
D4's Go changes; on this development machine that rebuild was intermittently unreliable when invoked
through `scripts/native/build.py`'s subprocess calls (the `go build` steps sometimes reported success
without actually writing the output file - resolved by killing stale Python processes that had the
previous DLL loaded via `ctypes.CDLL` and never released it, then retrying the build until both
artifacts were freshly written and verified by hash). This is a local build-environment quirk, not a
change to the build script or its documented usage.

### Phase D7 — Retrofit T01/T02/T07 and close T11/T12 properly

1. Move cleanup, single-file copy, and purge to async job execution internally.
2. Replace their synthetic process construction with `OperationResult` plus the compatibility wrapper.
3. Translate read/write failures through the execution-independent hierarchy.
4. Guarantee temporary output cleanup on every failure and cancellation.
5. Cover missing source, denied access, unsupported cleanup/purge, partial output, and relative local
   filename cases.

Exit gate: all Wave D rows share one lifecycle/result architecture and T11/T12 negative contracts are
verified.

### Phase D8 — Documentation and release compatibility

1. Document blocking and started-copy examples.
2. Document cancellation and timeout semantics.
3. Document `CompletedProcess` deprecation and which fields are CLI-only.
4. Document embedded arbitrary-option rejection.
5. Update ledger row statuses and parity file together.
6. State the planned release where blocking methods switch to `OperationResult`.

Exit gate: a caller can migrate without reading source or depending on execution-mode-specific types.

## 14. Test and verification plan

### 14.1 Pure unit tests

Use fakes and an injected monotonic clock; do not rely on real sleeps for state-machine tests.

Required groups:

- result/value invariants;
- exact RC request mapping;
- strict job/status and stats parsing;
- identity mismatch;
- start failure versus terminal failure;
- `check=True` and `check=False`;
- wait timeout without cancellation;
- cancellation before, during, and after terminal transition;
- repeated cancellation and close;
- job expiry before monitor observation;
- multiple waiters;
- monitor shutdown with active and terminal jobs;
- stats cleanup after final snapshot;
- no thread before the first started job;
- one monitor thread for many jobs;
- compatibility wrapper fields; and
- all `other_args`/`args` rejection paths.

### 14.2 Native local/memory tests

Run through the built DLL, not a fake binding:

- start returns before a deliberately nontrivial copy completes;
- status moves from running to a terminal state;
- stats are isolated by group;
- bytes/transfers become nonzero for real content;
- nested directory copy matches CLI results;
- destination-only files remain after copy;
- cancellation stops an active operation and permits subsequent RC calls;
- delayed `wait()` returns the monitor's cached terminal result;
- close cancels active owned jobs before finalization;
- relative local basenames work;
- no process is spawned; and
- repeated job cycles do not leak monitor threads or unbounded stats groups.

### 14.3 Go tests

Run at minimum:

```powershell
& "C:\Program Files\Go\bin\go.exe" test ./fs/rc/jobs ./fs/sync ./librclone/rclonekit/...
```

Also run the fork's normal focused tests for any package changed. Run race tests on Linux CI for the
downstream bridge/job endpoint even if Windows race builds are not part of the local loop.

### 14.4 Live provider parity

Local/memory tests cannot validate retry behavior, authorization failures, provider-side copies, or
the S3 bucket override. Run scoped live tests for:

- S3-compatible storage;
- Google Drive;
- one OAuth-backed provider through the intended authorization flow when available; and
- at least one transient/retryable failure scenario that can be induced safely.

Do not make live credentials mandatory for ordinary unit CI. Record the commit/platform/provider of
manual or protected-CI runs in the release evidence.

### 14.5 Platform matrix

Required before Wave D completion:

| Platform | Unit | Native build | Native tests | CLI parity | Live smoke |
| --- | --- | --- | --- | --- | --- |
| Windows amd64 | required | required | required | required | S3 or Drive |
| Linux x86_64 | required | required | required | required | S3 or Drive |

Wheel validation remains separate from source-tree success: install the built wheel into a clean
environment with no `rclone` on `PATH` and repeat the native smoke/copy/cancel test.

### 14.6 Standard verification commands

After every phase, run the smallest relevant subset. Before declaring Wave D complete, run:

```powershell
uv run ruff check .
uv run pyright src tests scripts _build_backend.py
uv run pytest tests\unit -q
uv run pytest tests\native -q
git diff --check
```

Then build/smoke both native artifacts using the repository scripts and run clean-wheel verification.

## 15. Acceptance criteria for Wave D

Wave D is complete only when all of the following are true:

1. T01-T05, T07, T11, and T12 have truthful machine-readable ledger states.
2. `copy`, `copy_dir`, and `copy_remote` work through the shared library without spawning an
   executable.
3. the public blocking methods retain their compatibility-release return type in both execution modes.
4. `start_copy()` returns a documented `JobHandle`.
5. a handle supports typed status, stats, wait, cancel, close, and context management.
6. job identity validates both execute ID and job ID.
7. a delayed caller receives cached terminal state despite rclone's expiry policy.
8. progress comes from the explicit accounting group, not global stats or a nonexistent job-status
   field.
9. `copy()`'s high-level retry behavior is proven, not only encoded in `_config`.
10. fatal and no-retry failures do not loop.
11. cancellation interrupts active work and retry sleep.
12. client close does not finalize while owned jobs are active.
13. native finalization reports failure instead of hiding unfinished jobs.
14. `OperationResult` contains attempts and final stats without fake subprocess details.
15. `check` behavior and error types are execution-independent at the public operation layer.
16. T11/T12 negative and temporary-file cleanup cases pass.
17. relative local basenames and the full path matrix pass.
18. S3 `no_check_bucket` parity is either implemented and tested or explicitly removed as a separate,
   approved behavior change.
19. Windows and Linux native matrices pass.
20. a clean installed wheel works with no `rclone` executable available.

## 16. Recommended commit structure

Suggested parent-repository commits:

1. `Add machine-readable C ABI migration coverage`
2. `Add operation result and job status types`
3. `Add typed RC job control`
4. `Add monitored embedded job handles`
5. `Update native rclone pin for retry-aware copy jobs`
6. `Add typed embedded transfer option encoding`
7. `Port copy operations to embedded jobs`
8. `Move long embedded mutations to job execution`
9. `Unify embedded transfer error handling`
10. `Document embedded job and result compatibility`

The native fork change is committed and tested independently with an rclone-style subject such as:

```text
librclone/rclonekit: add retry-aware asynchronous copy operation
```

Do not move the submodule pointer to a commit that exists only locally. Push the fork commit first,
verify it is fetchable, then update the parent pin together with the Python code that consumes it.

## 17. Final recommendation

Continue from the current branch; do not restart the migration. The existing boundaries, tests, and
no-fallback behavior are valuable and should be preserved.

Before writing T03-T05 adapters, implement the result model, RC job boundary, monitor, and downstream
retry-aware endpoint in that order. This resolves the real architecture once, then lets
`copy`, `copy_dir`, `copy_remote`, and the earlier mutating Wave D calls become small facade mappings.

The most important revision to the earlier plan is that `sync/copy + _config.Retries` is not semantic
parity with the CLI. The custom binary is already an accepted product component, so the optimal place
to preserve rclone's own retry classification is a narrow downstream RC endpoint, not Python guesses
or fake subprocess compatibility. Keep the public Python API operation-oriented, keep the C ABI
generic, and keep the additional Go code isolated under `librclone/rclonekit`.
