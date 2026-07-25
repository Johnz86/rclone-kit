# Post-migration follow-up work (temporary tracking doc)

Not part of the original 6-stage CLI-removal plan (A-F, now all complete).
Surfaced as findings during Stages C/D/E of the CLI-to-embedded migration
(`native-c-abi-migration` branch) and deliberately deferred rather than
folded into those stages' scope. This file exists to make sure they aren't
lost track of now that the plan itself is done - delete it once each item
below is either resolved or promoted into a tracked issue elsewhere.

Item 3 (the dead "Cache rclone release archive" CI step) was resolved by
Stage B's CI rework: both workflow files now build the real native library
in CI (submodule checkout, Go/llvm-mingw/WinFsp on Windows, a
manylinux2014 container on Linux) and the dead cache steps were replaced
with caches for the new toolchain downloads instead.

## 1. `CompletedProcess` return-type simplification

`src/rclone_kit/completed_process.py`'s `completed`/`stdout`/`stderr`/
`failed()`/`successes()`/`from_subprocess()` surface is CLI-subprocess-
shaped (constructs/reads a list of `subprocess.CompletedProcess[str]`).
Confirmed via grep during Stage E: **zero callers in `src/` anymore** -
every production call site was in the CLI backend, deleted in Stage C.
Only three test files still exercise `from_subprocess()`
(`tests/unit/test_command_security.py`, `tests/unit/test_completed_process.py`,
`tests/unit/test_upload_parts_server_side_merge.py`).

The class's own docstring already predicted this exact moment: "this is
itself deprecated in favor of returning `OperationResult` directly, once
the embedded-first major release removes the CLI backend." That release is
now. This is the same shape of work as Stage D (drop now-dead CLI-shaped
surface), but for a *return type* rather than *parameters* - it touches
every public `Rclone` method that currently returns
`CompletedProcess.from_operation_result(...)` (`copy_to`, `copy`,
`copy_dir`, `copy_remote`, `cleanup`, `delete_files`, `purge`, `copy_files`)
plus their tests, and the `rclone_kit.__init__` public export. Matches the
architecture review's finding #7 (see item 2 below) almost verbatim.

**Next step**: design what these methods return instead (`OperationResult`
directly? A list of them for `copy_files`?), then a Stage-D-shaped sweep.

## 2. Pre-existing embedded-path defects (`reference/review_rclone_implementation.md`)

Findings from an architecture review read during Stage C planning, entirely
independent of CLI removal - real defects in the embedded path itself, not
touched by this migration and not caused by it:

- **Process-global runtime ownership for production use** (finding #2):
  the native ABI permits initializing a given `RcloneRuntime` exactly once
  per process, never reinitializable. Tests already cope with this via a
  session-scoped shared runtime (`tests/native/conftest.py`,
  `tests/cloud/conftest.py`); a real multi-client production application
  has no equivalent guidance or tooling yet.
- **Job lifecycle bugs** (finding #4, `src/rclone_kit/job.py`): `stats()`
  freezes after its first nonterminal snapshot instead of refreshing;
  `cancel()` is documented as non-blocking but synchronously calls
  `job/stop` then `job/status`; a transient status/parsing/RC error
  permanently settles and forgets a job that may still be running;
  cancellation can be misclassified if a job fails independently after
  `cancel_requested=True`; a failed `close()` shutdown still stops the
  monitor thread, so retrying `close()` can't make progress; the monitor
  thread join has a 1s timeout whose success is never checked.
- **Weak native finalization** (finding #5): `RcloneRuntime.close()`
  ignores `RcloneKitFinalize`'s status/output; the Go implementation's
  `librclone.Finalize()` only runs GC and has an open TODO about
  unfinished async jobs - it doesn't stop jobs/streams/serves/mounts
  itself. `EmbeddedFilesStream` instances aren't tracked by `close()`.
  Disposed serve/mount handles stay in the client's tracking sets forever.
  Every native RPC call is serialized by one Python lock
  (`native/runtime.py`), so a blocking list-stream pull delays unrelated
  calls.
- **Linux path-modeling bugs** (finding #6): `to_path()`
  (`src/rclone_kit/util.py`) treats a Unix local path like `/tmp/data` as
  a remote named `/tmp/data`, producing `/tmp/data:` when reconstructed.
  `serve_http()`'s embedded adapter unconditionally splits its source on
  `":"`, so a Unix local path raises `ValueError` before the RC call.
  Every row in the (now-deleted) parity ledger recorded `linux = false` -
  Linux has never actually been exercised end-to-end against a real build.
- **Stale config snapshots** (finding #9): `Rclone.config` is a snapshot
  taken at construction; `is_s3()`/`get_s3_credentials()`/
  `encode_fs_spec()` all read that snapshot rather than rclone's live
  loaded config, so a `config/create`/`config/update` call the embedded
  runtime already knows about can be invisible to Python-side S3/credential
  logic until the client is rebuilt.
- **No OAuth/authorization workflow** (finding #3): the native fork has
  the underlying listener/redirect separation (`oauth_listen_addr`/
  `oauth_redirect_url`) but Python has no session API, callback relay,
  config-create workflow, or polling/cancellation wrapping it yet.

**Next step**: none of these block Stage B/F. Worth their own scoped
session(s) once the CLI-removal plan is fully closed out - probably one
per bullet given how different their surfaces are (job.py vs. runtime.py
vs. util.py vs. a net-new OAuth module).

## 3. `release.yaml` cache step is now dead weight

Resolved by Stage B - see the note at the top of this file.
