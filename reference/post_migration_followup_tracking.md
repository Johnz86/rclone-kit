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

Item 1 (`CompletedProcess` return-type simplification) is resolved: the
class and its dedicated test file are deleted, and `copy_to`/`copy`/
`copy_dir`/`copy_remote`/`cleanup`/`delete_files`/`purge`/`copy_files` all
return `OperationResult` directly (`copy_files()` returns one aggregated
`OperationResult`, not a list). See the "Public types still favor the
removed CLI model" finding in `review_rclone_implementation.md` (finding
#7) for the review that flagged this.

Item 2's job-lifecycle (finding #4), native-finalization (finding #5),
Linux-path-modeling (finding #6), runtime-ownership (finding #2), and
OAuth/authorization (finding #3) bullets are resolved - see their own
paragraphs below for what changed. Stale config snapshots (finding #9)
remains unstarted.

## 1. `CompletedProcess` return-type simplification

RESOLVED - see the note at the top of this file. `src/rclone_kit/
completed_process.py`'s `completed`/`stdout`/`stderr`/`failed()`/
`successes()`/`from_subprocess()` surface was CLI-subprocess-shaped
(constructed/read a list of `subprocess.CompletedProcess[str]`). The
class's own docstring already predicted this exact moment: "this is
itself deprecated in favor of returning `OperationResult` directly, once
the embedded-first major release removes the CLI backend." That release
was this pass - `copy_to`/`copy`/`copy_dir`/`copy_remote`/`cleanup`/
`delete_files`/`purge`/`copy_files` all return `OperationResult` directly
now, `CompletedProcess` and its dedicated test file are deleted, and the
three test files that still exercised `from_subprocess()`
(`test_command_security.py`, `test_completed_process.py` (deleted),
`test_upload_parts_server_side_merge.py`) were updated or removed to
match. Matches the architecture review's finding #7 (see item 2 below)
almost verbatim.

## 2. Pre-existing embedded-path defects (`reference/review_rclone_implementation.md`)

Findings from an architecture review read during Stage C planning, entirely
independent of CLI removal - real defects in the embedded path itself, not
touched by this migration and not caused by it:

- **Process-global runtime ownership for production use** (finding #2) -
  RESOLVED: the once-per-process constraint itself is a hard native-ABI
  limit, not something fixable in Python - but it's now backed by real
  guidance and tooling instead of neither. A new `native/runtime.py:
  shared_runtime()` (exported from `rclone_kit`) is a thread-safe,
  initialize-once accessor for the one `RcloneRuntime` a process can ever
  have, mirroring the pattern `tests/native/conftest.py`'s session-scoped
  fixture already used internally. `docs/production_usage.md`'s new
  "Runtime lifecycle and multi-client processes" section documents how to
  share it across many `Rclone` clients, the `self.config`-snapshot gotcha
  of passing `None` instead of the real config to a client built against
  an already-initialized runtime, and why true per-tenant isolation needs
  separate OS processes, not separate in-process runtimes.
- **Job lifecycle bugs** (finding #4, `src/rclone_kit/job.py`) - RESOLVED:
  `stats()` now always fetches fresh while a job is running, only using the
  cached snapshot once settled; `cancel()` dispatches `job/stop` and its
  follow-up poll on a background thread instead of blocking the caller on
  two RC round-trips; a transient status/parsing error (anything but
  `RcJobNotFoundError`) no longer settles/forgets the job, just retries on
  the next poll; cancellation is only classified `CANCELLED` when the
  terminal error text actually contains `"context canceled"` (matching
  what `job/stop` produces), not for any unrelated failure that merely
  races a cancel request; `shutdown()` only stops the polling thread once
  every job actually settled, so a failed `close()` leaves the thread
  running for a retry to make progress on, and checks the join itself
  succeeded rather than assuming it did.
- **Weak native finalization** (finding #5) - RESOLVED: `RcloneRuntime.close()`
  now logs a warning if `RcloneKitFinalize` returns a non-OK status instead
  of discarding it silently. `RcloneRuntime.call()` no longer serializes
  every RPC dispatch behind one Python lock - the Go bridge itself doesn't
  serialize `RPC()` calls, only briefly checks `initialized`, so the old
  lock just reintroduced a bottleneck the native layer never required;
  `close()` now waits for in-flight calls to drain (never interrupts them)
  before finalizing, and rejects new calls once started. `EmbeddedFilesStream`
  instances are now tracked and closed by `Rclone.close()`, matching
  `ServeHandle`/`MountHandle`. All three handle types now remove themselves
  from the client's tracking set on disposal via an `_on_dispose`/`_on_close`
  callback, instead of staying tracked forever.
- **Linux path-modeling bugs** (finding #6) - RESOLVED: `to_path()`/
  `RPath.__str__` (`src/rclone_kit/util.py`, `src/rclone_kit/rpath.py`) no
  longer treat a colonless local path (e.g. `/tmp/data`) as a remote named
  `/tmp/data` - a new `util.split_remote_name_and_path()` (shared with
  `listing_ops_embedded.py`'s matching helper, previously duplicated) sets
  an empty remote name for this case, and `RPath.__str__` omits the colon
  entirely when the remote name is empty, so the original path round-trips
  unchanged back through `RcPath.parse()` at the RC boundary.
  `serve_http()`'s embedded adapter now uses `partition(":")` instead of
  `split(":", 1)` + unpack, so a colonless local path no longer raises
  `ValueError` before the RC call. `Dir.to_string(include_remote=False)`
  had the same unconditional-split shape and is fixed the same way. Still
  only unit-tested (string-transform logic, isolated from the RC layer) -
  Linux has never been exercised end-to-end against a real build, so this
  is not itself a substitute for that.
- **Stale config snapshots** (finding #9): `Rclone.config` is a snapshot
  taken at construction; `is_s3()`/`get_s3_credentials()`/
  `encode_fs_spec()` all read that snapshot rather than rclone's live
  loaded config, so a `config/create`/`config/update` call the embedded
  runtime already knows about can be invisible to Python-side S3/credential
  logic until the client is rebuilt.
- **No OAuth/authorization workflow** (finding #3): RESOLVED - implemented
  against `docs/rclone_authorization_design.md`'s embedded-runtime redesign
  (the design doc's own prior "Status: Proposal, not implemented" note, and
  the stale CLI-subprocess version it superseded, are both now history; see
  that document for the current design). `src/rclone_kit/authorization/`
  (`types.py`, `state_driver.py`, `session.py`, `manager.py`, `relay.py`,
  `exceptions.py`) and `src/rclone_kit/rc/auth.py` drive `config/create`/
  `config/update`'s non-interactive state machine entirely through the
  existing shared `RcloneRuntime` - never a subprocess, never a second
  native library load. `AuthorizationManager.for_runtime()` enforces the
  single-flight queue rclone's process-wide OAuth globals require (see the
  design doc's "Central architectural constraint"); `Rclone.authorize(...)`
  is the thin `client.py` entry point, tracked for `close()` like
  `mount()`/`serve_webdav()`. Covered by unit tests (`tests/unit/
  test_rc_auth.py`, `test_authorization_*.py`) and by an offline integration
  test (`tests/native/test_authorization_offline_integration.py`) that
  drives a real OAuth round trip - a local fake provider, but the real
  vendored Go state machine, the real blocking `configSetup()` wait, and a
  real proof that a second session stays queued while the first is active -
  against the actual built native library. Live-provider verification
  (Phase 4 of the design doc's delivery plan) is also done, against a real
  Google Drive account in local-direct mode: `tests/live/
  gdrive_authorization/` (its own marker, `live_gdrive_authorization`,
  gated like `tests/live/gdrive`/`tests/live/s3` but additionally blocking
  on a human approving access in a browser - never run automatically) and
  `scripts/verify_gdrive_authorization.py` for a quicker manual check
  outside the test suite. A real reverse-proxy-shaped relay deployment
  against a caller-owned provider client remains unverified by this
  repository's own suite - see the design doc's Phase 4 notes.

**Next step**: job lifecycle, native finalization, Linux path-modeling,
runtime ownership, and OAuth/authorization (findings #4/#5/#6/#2/#3) are
done. Stale config snapshots (#9) remains unstarted; its interaction with
authorization is documented in `docs/production_usage.md`'s "Authorizing a
remote through rclone's own OAuth flow" section as a caveat, not a fix.

## 3. `release.yaml` cache step is now dead weight

Resolved by Stage B - see the note at the top of this file.
