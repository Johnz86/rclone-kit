# Native C ABI migration: Wave E design (filtered and partitioned operations)

Status: done. All 5 phases (E1-E5) complete: `parse_operation_attempts()` wired into `job.py`;
`TransferOptions` extended; `copy_files_embedded()`/`delete_files_embedded()` implemented and
dispatched from `Rclone.copy_files()`/`Rclone.delete_files()`; `fetch_size_files_embedded()`
implemented and dispatched from `Rclone.size_files()`; `tests/parity/coverage.toml` (T06/T08/L09),
the migration plan's Wave E status write-up, and `docs/production_usage.md` all updated. Every
decision in section 3 (E1-E9) was implemented as designed, with two corrections made empirically
during implementation rather than assumed up front:

- Section 4's working hypothesis for T08 ("a missing `FilesFrom` entry is not an error") was
  confirmed against the real DLL, and the same turned out to be true for T06's `rclonekit/copy` too
  (not explicitly predicted in section 4, but discovered the same way - empirically, via a native
  test - before being relied on).
- `group_files()` has a pre-existing quirk this design didn't anticipate: a flat, single-level
  `"remote:file"` reference (no subdirectory) loses its trailing `:` in the returned grouping key
  (`{"remote": [...]}` instead of `{"remote:": [...]}`), while any reference with at least one
  subdirectory level groups correctly (`{"remote:dir": [...]}`). This is a CLI-side bug inherited
  unchanged (not introduced by this wave, and out of scope to fix here) - native tests for T08 simply
  avoid the flat, no-subdirectory shape, matching `delete_files()`'s own documented usage (every
  example in `docs/production_usage.md` already nests under at least one directory level).

Date: 2026-07-23

Pinned native source: `native/rclone` at `d498adafa` on `rclone-kit/integration-v1`

Related documents:

- [Wave D review and design](native_c_abi_wave_d_review_and_design.md) (the architecture this wave
  builds on: `JobHandle`/`_JobMonitor`, `OperationResult`, `rclonekit/copy`, `RcPath`/`encode_fs_spec`)
- [CLI-to-C-ABI migration plan and ledger](rclone_cli_to_c_abi_migration_plan.md)

## 1. Scope

Ledger rows T06 (`copy_files`), T08 (`delete_files`), L09 (`size_files`). All three take a Python
`list[str]` of individual file paths (not a glob/filter expression) and, on the CLI backend, write
that list to a temporary `--files-from` file before invoking `rclone`. T06/T08 additionally
partition the list by common path prefix first, running one subprocess per partition so unrelated
files under different remotes/directories transfer in parallel.

This document is forward design (the code does not exist yet), unlike Wave D's document, which
reviewed already-written code. It exists because the ledger's one-line RC-method column predates
Wave D's own architecture decisions (`rclonekit/copy` superseding a bare `sync/copy`,
`OperationResult`/`JobHandle` as the only result shape), and because composite/partitioned
operations raise questions - partial-partition failure, aggregation, concurrency - that a one-line
ledger entry can't settle.

## 2. Facts confirmed from the pinned rclone source before designing

- `_filter.FilesFrom`/`FilesFromRaw`/`FilesFrom0` (`fs/filter/filter.go:151-153`) are `[]string` of
  **file paths on disk**, read and parsed by `filter.NewFilter`, not inline list values. The ledger's
  own L09 note ("temporary files are acceptable first") is correct and still applies to T06/T08 too:
  there is no way to hand rclone an in-memory file list over RC today.
- `_config`/`_filter` are parsed generically for **every** RC call, sync or async, in
  `fs/rc/jobs/job.go`'s `NewJob` (`rc.AddConfig`/`rc.AddFilter` at lines 259/264, before the
  method-specific `Fn` ever runs). So `_filter.FilesFrom` composes with `rclonekit/copy`,
  `operations/delete`, and `operations/list` identically - none of them need their own filter-aware
  code path.
- `operations/delete` (`fs/operations/rc.go:207`, `noRemote: true`) takes only `fs` and calls
  `operations.Delete(ctx, f)`, which walks `f` applying the ctx-scoped filter - exactly what
  `_filter.FilesFrom` needs to restrict.
- `operations/rmdirs` (`fs/operations/rc.go:206`) takes `fs`, `remote`, `leaveRoot`. The `delete`
  **command's** `--rmdirs` flag (`cmd/delete/delete.go:66-68`) is implemented as
  `operations.Rmdirs(ctx, fdst, "", true)` after `Delete` succeeds - i.e. `leaveRoot=true` always,
  called against the same fs root, `remote=""`. T08 reproduces this exact two-call sequence, not a
  new one.
- `rclonekit/copy`'s `Fn` (`librclone/rclonekit/rc/copy.go`) calls `sync.CopyDir(ctx, dstFs, srcFs,
  createEmptySrcDirs)`, which also honors the ctx-scoped filter. So the retry-aware endpoint Wave D
  built for `copy()`/`copy_dir()`/`copy_remote()` composes with `_filter.FilesFrom` for free - no Go
  changes needed for T06.
- `rclonekit/copy`'s output is `{"attempts": [...]}` (one entry per high-level retry attempt), which
  reaches Python as `JobStatus.output` (`rc/jobs.py`'s `_parse_job_status` already captures `output`
  verbatim) - but `job.py`'s `_settle_terminal` currently discards it, always setting
  `OperationResult.attempts = ()`. This is a pre-existing gap (noted, deliberately deferred, in Wave
  D Phase D7's status write-up) that this wave must close: T06's ledger note explicitly requires "one
  `OperationResult` containing attempts", and there is now a second, composite consumer of that field
  in addition to Wave D's already-shipped `start_copy()`/`copy()`.

## 3. Design decisions

### E1 - Reuse `group_files()` unchanged; temp files stay required

`rclone_kit/group_files.py`'s prefix-partitioning algorithm and `util.write_files_from()` are pure,
backend-agnostic string/filesystem helpers with no subprocess or RC coupling - both are reused
as-is. Each partition still gets its own temporary `--files-from`-style file
(`_filter.FilesFrom: [str(path)]`); nothing here removes the temp-file step the ledger already
flags as acceptable.

### E2 - T06 uses `rclonekit/copy` per partition, not a bare `sync/copy`

The ledger's RC-method column says "partitioned async `sync/copy` jobs"; that predates Wave D's
finding F1 (`_config.Retries` does not reproduce `rclone copy --retries` on a bare `sync/copy`).
`copy_files_partitioned`'s CLI implementation applies the same `--retries`/`--low-level-retries`/
`--retries-sleep` flags per partition as `copy_tree` does, so preserving that contract means each
partition must go through `rclonekit/copy`, exactly like `start_copy()`/`copy()` already do.

`TransferOptions`/`encode_transfer_options_config` (`operations/transfer_options.py`) gains four
fields not needed by Wave D's callers: `retries_sleep: str | None` -> `_config.RetriesInterval`,
`timeout: str | None` -> `_config.Timeout`, `max_backlog: int | None` -> `_config.MaxBacklog`,
`metadata: bool | None` -> `_config.Metadata`. These are plain `fs.ConfigInfo` fields
(`fs/config.go:601,610,651,685`) taking the same string/int/bool shapes as their CLI flag values, so
`copy_files_partitioned`'s existing string parameters (`retries_sleep`, `timeout`) pass straight
through unchanged. This extends the one existing options type rather than inventing a
files-specific parallel encoder.

### E3 - One aggregated `OperationResult` per call, not one per partition

`operation.py`'s `OperationResult.job_ids: tuple[int, ...]` was already designed for this (its own
docstring says so). `copy_files_embedded()`/`delete_files_embedded()` in
`transfer_ops_embedded.py` start every partition's job first (each `_JobMonitor.start_job(...,
check=False)` - never `check=True`, since raising out of the aggregation loop would abandon
still-running siblings and lose their results), then call `handle.wait(timeout=None)` on every
handle in turn, collecting each partition's `OperationResult`. A synthetic combined
`OperationResult` is then built directly (not via any `JobHandle`):

- `ok` = `all(partition.ok for partition in results)`
- `job_ids` = concatenation of every partition's `job_ids` (order = partition start order)
- `attempts` = concatenation of every partition's `attempts` (T06 only; T08's `operations/delete`
  emits no attempts, so this is always `()` for delete)
- `stats` = a summed `TransferStats` (bytes/checks/transfers/errors added; `speed`/`eta_seconds` are
  not meaningfully summable across finished jobs, so they are carried as `0.0`/`None` in the
  aggregate - only the cumulative counters matter once every partition has already settled)
- `warnings` = one `OperationWarning` per failed partition, carrying that partition's `source`/
  `destination`/`error` in `detail`, so a caller can tell exactly which partition(s) failed instead
  of only a joined string
- `error` = `None` if `ok`, else a summary joining each failed partition's `source -> destination:
  error` line - never just the first failure's message (the current CLI implementation's `for fut in
  futures: ... raise ValueError(...)` loop only ever surfaces the first result it happens to iterate,
  silently dropping any other partition's failure detail; this aggregation is a strict improvement,
  not a behavior this wave needs to preserve byte-for-byte)
- `cancelled` = `True` only if every non-ok partition was itself cancelled (mirrors
  `OperationResult`'s own invariant: `cancelled and ok` is impossible, and a mix of "some partitions
  cancelled, others merely failed" is still reported as `ok=False, cancelled=False` - cancellation is
  only claimed when it fully explains the aggregate outcome)

`copy_files_embedded()`/`delete_files_embedded()` accept `check` themselves and raise
`OperationFailedError(aggregate)` (or `OperationCancelledError`) only after every partition has been
collected - never mid-loop. This is the one behavioral departure from delegating straight to
`JobHandle.wait(check=True)`, and it is deliberate: partial-partition failure must not cut off
collection of the others.

No new public "start" method (no `start_copy_files()`) is introduced - the ledger does not ask for
one, and there is no existing precedent of exposing partition-level handles publicly. This can be
revisited in a later wave if a caller needs mid-flight progress across partitions.

### E4 - `max_partition_workers` becomes a start-batch size, not a thread pool

The CLI backend's `max_partition_workers` sizes a real `ThreadPoolExecutor`, because each partition
is a blocking subprocess that needs its own OS thread to run concurrently. An RC job is already
concurrent on rclone's side the moment `start()` returns - waiting for it from a single Python
thread does not serialize the underlying transfer. So the embedded path does not need a thread pool
at all for correctness; `max_partition_workers` is reinterpreted as how many partition jobs may be
outstanding (started but not yet waited-on) at once: start up to that many, wait for the whole
batch, then start the next batch. Default (`None`) starts every partition up front, matching
`delete_files`'s existing default (`max_partition_workers=None` already means "unbounded" for
`ThreadPoolExecutor` too). This keeps the parameter meaningful (bounding how many concurrent
in-flight jobs/accounting groups exist at once, still a real resource control) without pretending
Python-side threading is doing anything for an already-async RC call.

### E5 - T08: `operations/delete` then `operations/rmdirs(leaveRoot=True)`, same aggregation as E3

Partitioning for delete reuses `group_files()`'s remote-grouping exactly as
`delete_files_partitioned` does today. Per partition: start `operations/delete` with `fs =
encode_fs_spec(config, group_root)` and `_filter.FilesFrom` pointing at that partition's temp file;
`_config.Checkers`/`_config.Transfers` are set to the same historical `1000`/`1000` the CLI path
hardcodes (not new public parameters). If `rmdirs=True` and the delete job's own result is `ok`,
follow with a second job, `operations/rmdirs` (`fs=group_root`, `remote=""`, `leaveRoot=True`), and
fold its result into that partition's contribution to the aggregate - a partition that fails
`delete` never attempts `rmdirs` (there is nothing to clean up if delete didn't finish). Both calls
per partition still run through `_JobMonitor` (consistent with every other Wave D/D7 row), even
though neither needs retries - this keeps every ledger row on the same lifecycle machinery per
Wave D's D7 exit gate ("all Wave D/E rows share one lifecycle/result architecture").

### E6 - L09 is one direct, synchronous RC call - no job engine involved

`size_files()` never partitions today (a single `lsjson --files-from` covers every requested file
in one call, regardless of how many remotes/directories they span - only T06/T08 partition, because
only they *write*, and a write per remote/directory boundary is the thing that needs isolating).
`fetch_size_files_embedded()` in `listing_ops_embedded.py` follows the existing direct-call pattern
(`fetch_ls_embedded`, `stream_diff_embedded`): one `operations/list` call with `opt.filesOnly=True`,
`opt.recurse=True`, `_filter.FilesFrom=[str(tmp_path)]`, and `_config.UseListR=True` when
`fast_list=True` (mirroring `stream_diff_embedded`'s existing `fast_list` -> `UseListR` mapping, and
keeping the existing "don't recommend `--fast-list` here" warning). The existing sub-2-files
shortcut delegates to the already-embedded `fetch_size_file_embedded()` (`operations/stat`,
shipped in an earlier wave) unchanged. No `_JobMonitor`/`JobHandle` involvement: this is a
`decision = "direct_rc"` row, not `"composite_rc"`, exactly as the ledger already states.

### E7 - Public return-type impact: minimal, deferred to Wave I

- `copy_files()` keeps its declared `-> list[CompletedProcess]`. The embedded path returns a
  **single-element** list wrapping `CompletedProcess.from_operation_result(aggregate)` - the ledger's
  "one `OperationResult`... not a list of processes" concern is about the internal data model (one
  real result carrying every partition's `job_ids`/`attempts`, not N synthetic per-partition
  `subprocess.CompletedProcess` objects), not about the outer Python container shape. Changing
  `copy_files()`'s public signature to return a bare `CompletedProcess` is a compatibility-surface
  decision that belongs to Wave I ("public compatibility transition"), not this wave - consistent
  with Phase D7 keeping every method's existing public signature untouched.
- `delete_files()` already returns a single `CompletedProcess` on the CLI backend (it aggregates
  partitions internally today); the embedded path matches that shape with no change needed.
- `size_files()` returns `SizeResult`, a plain backend-agnostic dataclass; unaffected either way.

### E8 - Close the `OperationResult.attempts` gap generically, in `job.py`

`rc/jobs.py` gains `parse_operation_attempts(output: Mapping[str, object]) ->
tuple[OperationAttempt, ...]` (wire parsing stays in `rc/jobs.py` per that module's own docstring
convention), reading `output.get("attempts")` and validating each entry's `number`/`startTime`/
`endTime`/`duration`/`ok`/`error`/`fatalError`/`retryError` fields against `copy.go`'s `copyAttempt`
JSON tags. `job.py`'s `_settle_terminal` calls it unconditionally (`status.output` for any method
without an `"attempts"` key yields `()`, exactly today's behavior) instead of hardcoding `attempts=()`.
This is a small, generic fix at the one place `OperationResult` is constructed, not a per-operation
special case - and it also finally populates `attempts` for Wave D's own `start_copy()`/`copy()`,
closing the gap the Phase D7 status note flagged as deliberately out of scope for that phase.

## 4. Failure-contract and test coverage plan

- T06: missing source file among a valid list (that partition fails, others still copy and are
  reflected `ok=True` in the aggregate; overall `ok=False`); empty file list (no-op, matches today's
  `return []`/equivalent embedded no-op - encode as `ok=True`, `job_ids=()`, no jobs started at all,
  same as the CLI early return); duplicate entries in the input list; `other_args` rejection
  (`UnsupportedEmbeddedOperationError`, same as every other embedded row); a remote-qualified entry
  (`":"` in a path) still raises `ValueError` up front, unchanged, before any job starts.
- T08: missing file in the list (needs empirical confirmation against the real DLL before relying
  on it - the working hypothesis, from `Delete`'s filter-driven walk, is that a files-from entry
  naming a nonexistent file is simply not visited rather than raising, matching today's CLI
  behavior, but this must be verified with a real test before the failure contract is written down
  as `never raises` for this case); `rmdirs=True` leaves the root directory itself intact;
  `rmdirs=True` with a failed delete partition skips that partition's `rmdirs` call; empty file
  list no-op.
- L09: empty file list (existing `SizeResult(prefix=src, total_size=0, file_sizes={})` short-circuit,
  unchanged); single/double-file shortcut still routes through `fetch_size_file_embedded`; duplicate
  entries in the input (existing dedup-with-warning behavior preserved); a listed path outside `src`
  still raises `ValueError` (existing invariant, unchanged).
- All three: native-DLL parity tests in `tests/native/`, following the existing
  `test_transfer_ops_embedded_integration.py`/`test_listing_ops_embedded_integration.py` pattern
  (real temp directories, real built library, no mocks); unit tests in `tests/unit/` using the same
  `FakeJobClient` harness `test_transfer_ops_embedded.py` already established, extended to start
  multiple jobs per call.

## 5. Phase breakdown

- **Phase E1** - Extend `TransferOptions`/`encode_transfer_options_config` (E2); add
  `parse_operation_attempts` and wire it into `job.py._settle_terminal` (E8); unit-test both in
  isolation before any partitioned-operation code depends on them.
- **Phase E2** - Implement `copy_files_embedded()` (E2/E3/E4/E7) and dispatch it from
  `Rclone.copy_files()`; unit + native tests per section 4.
- **Phase E3** - Implement `delete_files_embedded()` (E5/E3/E4) and dispatch it from
  `Rclone.delete_files()`; unit + native tests per section 4.
- **Phase E4** - Implement `fetch_size_files_embedded()` (E6) and dispatch it from
  `Rclone.size_files()`; unit + native tests per section 4.
- **Phase E5** - Update `tests/parity/coverage.toml` (T06/T08/L09 rows) and the migration plan's
  Wave E status write-up together; document the new `TransferOptions` fields and the
  `attempts`-population fix in `docs/production_usage.md`.

Exit gate: T06/T08/L09 all reach `cli_parity_tested` with `failure_contract_complete = true`; no
new thread pool exists in the embedded path; `OperationResult.attempts` is populated for every
`rclonekit/copy`-backed call, embedded or composite.
