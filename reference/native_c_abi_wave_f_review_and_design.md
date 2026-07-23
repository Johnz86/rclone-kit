# Native C ABI migration: Wave F design (streaming and bytes)

Status: done. L02/L03/L04/T09/T10/T13 all reach `cli_parity_tested` in
`tests/parity/coverage.toml`, with dedicated native-DLL parity tests for
every row (including the two that turned out to need zero new code, T09
and L04) rather than leaving "already works transitively" as an inferred,
untested claim. A pre-existing bug in `rclone_kit.db.models.
create_file_entry_model` (a single `Column` instance shared across every
dynamically-created table's `FileEntryConcrete` subclass, crashing with
`ArgumentError` the second time two different tables were created in one
process) was found and fixed while writing L03's native test - unrelated
to embedded/Wave F specifically, but blocking a clean full-suite run once
a second `save_to_db()`-shaped test existed alongside `test_db.py`.

Date: 2026-07-23

Pinned native source: `native/rclone` at `6c929caad` on `rclone-kit/integration-v1` (local-only per
the standing constraint; not pushed to the fork remote without separate authorization)

Related documents:

- [Wave D review and design](native_c_abi_wave_d_review_and_design.md)
- [Wave E review and design](native_c_abi_wave_e_review_and_design.md)
- [CLI-to-C-ABI migration plan and ledger](rclone_cli_to_c_abi_migration_plan.md)

## 1. Scope

Ledger rows L02 (`ls_stream`), L03 (`save_to_db`, transitive from L02), L04 (`print`, transitive
from T11/T12), T09 (`write_bytes`), T10 (`write_text`, transitive from T09), T13 (`copy_bytes`).
D10/D11 are **not** action items for this wave - they are the migration plan's own internal/
distribution-removal ledger, and both name their gate as "L02/L03 ... pass" / "L12 ... pass": they
record when it becomes safe to *delete* `FilesStream`'s CLI-only process-stdout parser and
`diff_stream_from_running_process`, not something to implement now. That removal belongs to Wave I.

## 2. What was already done, discovered by checking before assuming

- **T09/T10 were already functionally complete.** `write_bytes()` already calls
  `self.copy_to(str(tmpfile), dst, check=True)`, and `copy_to()` has dispatched to
  `copy_file_to_embedded()` since Wave D Phase D7; `read_bytes()`/`read_text()`'s exception
  handling already covers both `subprocess.CalledProcessError` and `OperationFailedError`.
  Confirmed empirically against the real DLL (`embedded.write_bytes(...)`/`write_text(...)`
  round-tripped correctly with no code changes) before writing a single line for this wave -
  the only remaining work is a dedicated parity test and a ledger status update.
- **L04 (`print`) is already functionally complete** for the same reason: `print_contents()` only
  calls `read_text()`, already embedded since Wave D.
- **L12 (`diff`) already has a working embedded implementation** (`stream_diff_embedded`, built in
  an earlier wave): `operations/check`'s report arrays are a bounded summary of *differences*, not
  a full recursive listing, so materializing them in one RC response was never the problem D11
  describes - only L02 (a full listing, potentially millions of entries) is.

So the genuinely new engineering in this wave is: L02's streaming list bridge, and T13's byte-range
bridge.

## 3. Why `operations/list` cannot back `ls_stream()`

`fs/operations/rc.go`'s `rcList` (backing `operations/list`) calls `ListJSON` with a callback that
appends every item to a Go slice, then returns the *whole* slice as one `rc.Params["list"]` - read
directly from source, not assumed. There is no cursor, continuation token, or paging parameter
anywhere in this call. For "millions of entries" (this wave's own stated stress-test bar), returning
one unbounded JSON blob is the exact problem L02 says a bridge must avoid.

## 4. Design decisions

### F1 - A new downstream RC method family, not an ABI change

`librclone/rclonekit/rc/liststream.go` adds three RC methods - `rclonekit/liststream/open`,
`.../next`, `.../close` - reusing the *existing* `RcloneKitRPC` C function exactly like `rclonekit/
copy` did in Wave D. No C ABI change was needed: a stateful cursor is just a small handle table
(`map[int64]*listStreamState`, guarded by a mutex) private to that Go package, keyed by a
process-local, monotonically increasing `streamId` - conceptually parallel to `fs/rc/jobs.Jobs`
tracking `Job`s by ID, but independent of it (a listing stream is not a job: it has no retry
concept, and needs to hand back partial results before "finishing", which `job/status.output` -
populated only once, when a job's `Fn` returns - structurally cannot do).

### F2 - Bounded channel for backpressure, not an unbounded buffer

`open()` starts a background goroutine running `operations.ListJSON` with a callback that sends
each item into a 1024-capacity buffered channel (`listStreamBufferSize`); once full, the producer
blocks until the consumer pulls more via `next()`. This is the actual bounded-memory guarantee L02
asks for: at most ~1024 items are ever held in memory ahead of the consumer, regardless of how many
millions of entries exist in total.

### F3 - `next()` is bounded-wait, not indefinite-block, and not poll-only

`RcloneRuntime.call()` serializes every RC dispatch through one internal lock (its own docstring:
"`call` serializes RPC dispatch with an internal lock"). If `next()` blocked indefinitely waiting
for a slow backend, no other RC call - including an emergency `close()` from another Python thread -
could get through the SAME lock to interrupt it. So `next(streamId, maxItems, timeoutMs)` uses a
`time.Timer` internally: it returns whatever accumulated (possibly zero items, `done=false`) once
`timeoutMs` elapses, exactly like `_JobMonitor`'s own poll loop bounds each wait rather than blocking
forever. Unlike job polling, though, this is a true *pull*: if items are already available, `next()`
returns immediately with up to `maxItems` of them, so a fast producer/consumer pair never pays the
full timeout per call - only a genuinely idle wait does.

### F4 - Cancellation via the callback's own `select`, not a separate drain step

`open()` derives `streamCtx` (with its own `cancel`) from the incoming, already-`_config`/`_filter`-
parsed `ctx` (see F5 for why that parent choice is safe) - not from `context.Background()` the way an
`_async` job detaches. The `ListJSON` callback does `select { case state.items <- item: ...; case
<-streamCtx.Done(): return streamCtx.Err() }`, so the moment `close()` calls `cancel()`, a producer
that is blocked trying to send (channel full) immediately unblocks via the cancelled-context branch
and the walk stops - no separate "drain the channel so the producer doesn't leak" step was needed,
confirmed by an early-cancellation probe against the real DLL that returned promptly.

### F5 - The RC call's own per-call context is a safe parent for a goroutine that outlives the call

For a *synchronous* RC call (no `_async: true`), `fs/rc/jobs.Job.run(ctx, fn, in)` runs `fn` on the
calling goroutine and returns; nothing calls that specific job's `cancel()` afterward unless
something later calls `job/stop` on its ID - which `rclonekit/liststream/open` never exposes, since
its own numbering (`nextStreamID`) is entirely separate from `fs/rc/jobs`'s job IDs. Go contexts do
not expire simply because the function that created them returned; a context stays valid as long as
something (here, the background goroutine) holds a reference and its `cancel` is never called. So
capturing the incoming `ctx` directly - rather than detaching to `context.Background()` - is safe and
has the added benefit of carrying over `_config`/`_filter` for free, since those were already
attached to that exact `ctx` by `fs/rc/jobs.NewJob` before `rcListStreamOpen` ever ran.

### F6 - `next()`'s "done" is derived from the channel close itself, never from a racy flag alone

`state.err` is written by the producer goroutine strictly before it closes `state.items`. Go's
memory model guarantees a channel close happens-before any receive that observes it via `ok=false`,
and both actions occur in program order in the same goroutine - so a consumer that sees `ok=false`
is guaranteed to see the error write too, without needing the mutex for that specific ordering (the
mutex is kept anyway, for defensive consistency against any future concurrent-access case). `done`
in the RC response is only set `true` in the exact call where the receive itself observed the closed
channel - never inferred from a separately-read flag that could race with the close.

### F7 - Item wire shape is untouched, so parsing is reused, not duplicated

`rcListStreamOpen` uses the same `operations.ListJSONItem`/`ListJSONOpt` types `operations/list`
already uses, so each item's JSON shape (`Path`/`Name`/`Size`/`MimeType`/`ModTime`/`IsDir`/...) is
byte-identical to what `listing_ops_embedded.fetch_ls_embedded` already parses via `RPath.from_dict`/
`RcloneJsonEntry`. The Python-side stream wrapper reuses that exact parsing function rather than
inventing a second one for the same shape.

### F8 - `copy_bytes`/`T13`: a single bounded RC call, not a cursor

Unlike a listing (unbounded item count), a byte range is bounded by construction (`offset`/`count`
are both caller-supplied, finite numbers) - there is nothing to page through. `rclonekit/readrange`
(`librclone/rclonekit/rc/readrange.go`) is one RC method taking `fs`/`remote`/`offset`/`count`/
`outputPath`, opening the object with `fs.RangeOption{Start: offset, End: offset+count-1}` (HTTP
Range semantics - inclusive end, confirmed from `fs/open_options.go`) and streaming directly into a
local file via `io.CopyN`, so a large range is never held in memory or base64-encoded into the RC
response the way a bare `operations/copyfile`-shaped call would have to. `count == 0` is special-
cased to create an empty output file without ever calling `Object.Open` - avoiding an
inverted/degenerate `RangeOption{Start: offset, End: offset-1}`.

`io.CopyN` returns `io.EOF` whenever it copies fewer bytes than requested - including the entirely
expected case of a range that extends past the object's actual end. That is treated as success, not
failure: `bytesWritten` simply reports the real (possibly shorter) count. This was verified
empirically against the real DLL for an exact range, a zero-length range, a range starting exactly
at EOF, a range extending past EOF, and a full-file range - all five produced the expected byte
content with no error, and a genuinely missing source object raised `RcCallError` as expected.

Because the request is bounded and already fits the existing "one RC call, `check` decides whether
failure raises" shape T01/T02/T07 established, `copy_bytes()`'s embedded path is *routed through the
same `_JobMonitor`* as those rows (an async job, not a bare synchronous call) so a large in-flight
range download can be observed/cancelled/timed-out the same way any other embedded operation can,
rather than blocking the runtime lock for the whole transfer the way a synchronous call would
(the exact problem Wave D's finding F5 already fixed for T01/T02/T07).

## 5. Public API shape

- `ls_stream()` keeps its existing signature and return type (`FilesStream`-shaped: `.files()`,
  `.files_paged()`, context-manager close). Embedded execution returns a stream object satisfying the
  same duck-typed surface, backed by `rclonekit/liststream/*` instead of a subprocess; `save_to_db()`
  needs zero changes (it only ever calls `ls_stream()` and `.files_paged()`), matching its ledger
  "transitive" categorization.
- `copy_bytes()`/`copy_byte_range` keep their existing signature and return type (`None`; writes to
  `outfile`). Embedded execution raises `OperationFailedError`/`OperationCancelledError` on failure
  when `check` resolves `True` (matching every other embedded row's `check` contract), or leaves a
  possibly-short/partial local file when `check=False` - callers needing the exact byte count get it
  from the `OperationResult` if they call the lower-level embedded function directly, but the public
  `copy_bytes()` signature does not change to expose that (it never returned a count on the CLI
  backend either).

## 6. Failure-contract and test coverage plan

- L02: empty listing, single-item listing, listing larger than one buffer/batch, `max_depth`
  variants, cancellation mid-stream (`close()` before exhaustion), `files_paged()` batching,
  `save_to_db()` transitively.
- T13: exact range, zero length, range starting at EOF, range extending past EOF, full-file range,
  missing source, `check=True` raising, `check=False` leaving a partial file.
- T09/T10/L04: dedicated parity tests confirming the already-working transitive behavior, so this
  is proven rather than merely inferred from reading `copy_to()`'s dispatch.
