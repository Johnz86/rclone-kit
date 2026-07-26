# Progress monitoring: design proposal

Status: implemented. `JobHandle.watch()`/`.on_progress()`,
`download_multi_threaded()`'s `on_progress` parameter, and
`start_copy_files()`/`start_delete_files()` + `PartitionedJobHandle` (the
"Do now" and "Do soon" phases below) have all landed; the native push
callback described in "Bigger bet, validate before committing" remains
deliberately deferred. This document supersedes an earlier,
narrower proposal that only sketched a live test around today's manual
polling loop. Progress monitoring is a first-class feature of this
library, not an afterthought to be tested around - this document is a
first-principles design for the feature itself, informed by a full
architectural review of both the Python client and the vendored native
Go bridge it embeds.

## Recommendation, up front

Keep the existing `JobHandle` polling primitives exactly as they are -
they are sound and already released (v1.0.0/v1.1.0). Add a purely
Python-side ergonomic layer on top (`watch()`, `on_progress()`), extend
progress tracking to the two places that currently have none
(`copy_files()`/`delete_files()`'s partitioned jobs, and
`download_multi_threaded()`'s chunked HTTP downloads), and leave a
genuine native push-callback mechanism as an explicitly deferred, opt-in
future track - not because it is a bad idea, but because the investigation
below shows it is a materially larger and riskier undertaking than it
first appears, for a benefit that has not yet been shown to be necessary.

Nothing here deprecates or breaks anything. Every change is additive.

## How progress monitoring works today

`rclone-kit` executes every operation through an embedded native shared
library (`librclone_kit`) over a synchronous ctypes bridge: Python calls
`RcloneRuntime.call(method, params_json)`, which crosses into Go via a C
ABI function and returns `(status, output_json)` synchronously. There is
no persistent connection and no callback mechanism registered across that
boundary anywhere today.

On top of that, `start_copy()` (`src/rclone_kit/client.py`) starts an
async rclone RC job and returns a `JobHandle` (`src/rclone_kit/job.py`),
backed by a `_JobMonitor` that polls rclone's own `job/status` and
`core/stats` RC methods on a background thread. `JobHandle` exposes
`.status()`, `.stats()`, `.wait()`, `.cancel()`, and `.done`.
`docs/production_usage.md`'s "Real asynchronous control with
`start_copy()`" section documents the released, user-facing contract:
callers write their own loop -

```python
while not handle.done:
    stats = handle.stats()
    print(f"{stats.bytes}/{stats.total_bytes} bytes, {stats.transfers} files")
    time.sleep(5)
```

This is a real, already-published API. Any redesign has to treat it as
load-bearing, not a draft to be thrown away.

## Is polling the right model, or should progress push instead of being pulled?

This was the central question of the architectural review, and the answer
is: **there is nothing to push from yet - it would have to be built.**

Tracing the exact mechanism rclone's own `--progress` CLI flag uses (on the
theory that if a live-updating terminal display already exists, something
underneath it must already be a push/listener mechanism worth reusing)
leads to `native/rclone/cmd/progress.go`'s `startProgress()`. It is itself
just a `time.NewTicker(500ms)` goroutine that calls
`accounting.GlobalStats().String()` on every tick and repaints the
terminal - i.e. polling, implemented in Go, of the exact same
mutex-protected `StatsInfo` structure that `core/stats`'s RC handler
(`native/rclone/fs/accounting/stats_groups.go`) reads on demand. The
Prometheus exporter in the same package does the same thing again. Every
existing consumer of transfer stats in this codebase - the CLI progress
bar, the RC endpoint this library already calls, and the Prometheus
exporter - is a *poller* of the same data. The finest granularity the
system ever produces natively is a ~1-second-averaged speed figure (each
`Account`'s own `averageLoop()` ticker), with byte counters updated
synchronously per read syscall underneath that. There is no "notify me
when this changes" hook anywhere to attach to.

Building one from scratch is a bigger undertaking than it sounds, because
of what the current native ABI deliberately does *not* do. Reading the
actual ABI surface (`native/rclone/librclone/rclonekit/abi.h`,
`bridge/bridge.go`, `main.go`) confirms it is a narrow, one-directional
request/response surface - every exported function
(`RcloneKitRPC`/`RcloneKitInitialize`/`RcloneKitBuildInfo`/
`RcloneKitFinalize`) is synchronous, and Go never calls back into the
host language anywhere today. A registered-callback mechanism would
introduce several new categories of problem at once, not just new
functions:

- **The first-ever reverse-direction call** in this codebase: Go invoking
  a C function pointer requires a small cgo trampoline (a compiled-in
  `.c`/`.h` helper), since cgo only lets C call Go without one, not the
  reverse. This is new plumbing, not an incremental addition to `main.go`'s
  existing `//export` pattern.
- **A new ABI version.** This is a genuinely new capability, not a
  refinement of an existing one - `RCLONEKIT_ABI_VERSION` would need to
  bump, and older/newer library-vs-binding combinations negotiated (the
  `AbiVersionMismatchError` machinery in `native/runtime.py` already
  models exactly this kind of check).
- **A new lifecycle/ownership hazard.** Nothing on the Go side holds a
  pointer into the Python process between calls today. A registered
  `ctypes.CFUNCTYPE` callback is exactly that: if Go invokes it after the
  Python interpreter starts finalizing (process shutdown,
  `RcloneRuntime.close()`), the process crashes rather than raising a
  catchable exception. `RcloneRuntime.close()`'s existing "wait for
  in-flight calls, then finalize" design would need extending to
  guarantee every registered callback is unregistered and no goroutine is
  still mid-callback before `Finalize()`/interpreter teardown.
- **A GIL-blocking hazard in the callback body**, not just in the call
  itself. ctypes handles GIL reacquisition transparently for a callback
  invoked from a foreign OS thread, so the cross-language call is safe.
  But whatever Python code runs inside the callback runs *while holding
  the GIL*, on a thread Go controls - a slow or blocking user callback
  would stall every other Python thread in the process, not just the one
  job being watched. This is exactly the footgun `job.py`'s own docstring
  already calls out for the existing poll thread ("no user code runs on
  the monitor thread"). A safe native design must therefore never invoke
  user code directly from the trampoline - only enqueue a snapshot for a
  Python-owned thread to dispatch - which is meaningfully more
  implementation work for a payload that is, in the end, the same
  `core/stats` JSON delivered on a different schedule.
- ctypes also silently swallows exceptions raised inside a callback
  (routed to `sys.unraisablehook`, never propagated into C) - not a
  crash risk, but a footgun this library would need to explicitly guard
  against with its own wrapping/logging.

Given all of this, a native push mechanism is a large, cross-repository
undertaking (touching `native/rclone`, which has its own git history,
`AGENTS.md`, and release cadence, independent of this repo) whose only
concrete benefit over polling is shaving sub-second latency and a
background thread's poll overhead - against data that server-side is
already only refreshed at ~1-second granularity, produced by a call
(`core/stats`) that is cheap (an in-memory struct read, not I/O). This is
a **bigger bet, to validate before committing** - see "Delivery phases"
below - not the answer this document leads with.

## Recommended API design

### `JobHandle.watch()` and `JobHandle.on_progress()`

The existing primitives are correct and stay untouched. What's missing is
purely ergonomic: every caller today hand-writes the poll loop, which is
boilerplate, easy to get subtly wrong (e.g. calling `.stats()` again after
`.done` already flipped true, or picking too tight a poll interval), and
offers no callback/background-thread option at all. Two new, additive
methods on `JobHandle`:

```python
def watch(self, *, interval: float = _DEFAULT_WATCH_INTERVAL_SECONDS) -> Iterator[TransferStats]:
    """Yield a snapshot every `interval` seconds until the job settles.
    The final snapshot is always yielded last. A thin wrapper around
    stats()/done - encapsulates the sleep loop, nothing more."""


def on_progress(
    self,
    callback: Callable[[TransferStats], None],
    *,
    interval: float = _DEFAULT_WATCH_INTERVAL_SECONDS,
) -> ProgressSubscription:
    """Run `callback` on a dedicated background thread every `interval`
    seconds until the job settles. Never runs on `_JobMonitor`'s shared
    poll thread, so one job's slow callback can never delay another
    job's status polling. A callback exception is logged and swallowed,
    never crashes the thread. Returns a handle whose `.stop()`/context-
    manager exit ends the subscription early."""
```

Both are implemented entirely in terms of the existing `.stats()`/`.done`
- no `_JobMonitor` internals change, no native changes needed. `watch()`
is a plain generator; `on_progress()` is a small dedicated-thread wrapper
around it, consistent with the existing "no user code on the shared
monitor thread" rule.

### Extending progress to partitioned and HTTP transfers

Two real gaps exist today, both fixable without touching the native layer:

- **`copy_files()`/`delete_files()`** (`src/rclone_kit/operations/
  transfer_ops_embedded.py`) start N per-partition jobs and immediately
  `.wait()` on each - there is no non-blocking handle to watch at all.
  Add `start_copy_files()`/`start_delete_files()` to `client.py`,
  mirroring the existing `start_copy()`/`copy()` relationship: they
  return a `PartitionedJobHandle` composing the constituent per-partition
  `JobHandle`s (a pure Python aggregation wrapper, reusing the existing
  `_sum_stats()` logic for its own `.stats()`). `copy_files()`/
  `delete_files()` become thin `start_copy_files(...).wait()`-style
  wrappers, exactly as `copy()` is over `start_copy()` today. To avoid
  duplicating `watch()`/`on_progress()` between `JobHandle` and
  `PartitionedJobHandle`, define a small `Protocol` (`done: bool`,
  `stats() -> TransferStats`) - matching this codebase's existing
  narrow-Protocol pattern (`RcJobClient` in `rc/jobs.py`) - and implement
  `watch()`/`on_progress()` once against that Protocol.
- **`download_multi_threaded()`** (`src/rclone_kit/http_server.py`) has
  zero progress reporting today despite doing real chunked network I/O
  through a `ThreadPoolExecutor`. This has no relationship to the RC job
  machinery at all: add an optional
  `on_progress: Callable[[int, int], None] | None = None` parameter
  (bytes completed, total bytes), invoked from the existing completion
  loop as each chunk future resolves. No new concurrency model, no native
  changes, fully backward compatible (defaults to `None`).

`ls_stream()` is deliberately left out: the caller already controls
iteration and page size directly, and there is no natural "total" to
report a percentage against for an unbounded recursive listing. A
documentation note (count `len(page)` across `files_paged()` yourself) is
enough; no new API surface.

### Async/await is out of scope

The whole library - including the ctypes bridge itself - is synchronous.
`_JobMonitor` already solves "don't block the caller's thread while
progress accrues" with a plain background thread, which composes fine
with any external progress UI (tqdm, structlog, a Prometheus gauge, a
GUI progress bar) without asyncio. Adding an async surface for this one
feature would be inconsistent with the rest of `rclone_kit` and would
require wrapping every underlying synchronous call for no benefit this
design doesn't already provide via `on_progress()`'s background thread.

### The data model needs no changes

`TransferStats`/`ActiveTransfer`/`OperationResult`
(`src/rclone_kit/operation.py`) already carry everything both the pull-side
wrapper and a hypothetical future push payload would need - `bytes`/
`total_bytes`, `speed`, `eta_seconds`, per-file detail via
`active_transfers`, and summability across partitions via the existing
`_sum_stats()`. This reinforces that the redesign is a *delivery-mechanism*
change, not a data-model change.

## Backward compatibility

Fully additive, no deprecation:

- `JobHandle.status()/.stats()/.wait()/.cancel()/.done` keep their exact
  current signatures and behavior forever. The documented
  `while not handle.done: ...` loop keeps working byte-for-byte, and
  remains the right tool for a caller that wants to interleave stats
  checks with other work on its own thread (e.g. inside a UI event loop)
  rather than receive a callback on a background thread.
- `watch()`/`on_progress()` are new methods; nothing existing changes
  shape.
- `start_copy_files()`/`start_delete_files()` are new entry points;
  `copy_files()`/`delete_files()` keep their current signatures and
  `OperationResult` return type unchanged.
- `download_multi_threaded()`'s new `on_progress` parameter defaults to
  `None`.

`docs/production_usage.md` should gain `watch()`/`on_progress()` as the
recommended idiom going forward, while keeping the manual polling loop
documented as the always-available low-level primitive - not deprecated,
since it is genuinely still the right tool for some callers.

## Delivery phases

**Do now** - low risk, Python-only, no `native/rclone` changes:

- `job.py`: `JobHandle.watch()` / `.on_progress()`, plus a small
  `ProgressSubscription` helper (its own stop event/thread, not reusing
  `_JobMonitor`'s thread).
- `http_server.py`: `on_progress` parameter on `download_multi_threaded()`.
- `docs/production_usage.md`: document the new methods as the recommended
  idiom.
- Live test example (`tests/live/s3/`): demonstrate `handle.watch()` as
  the primary idiom, keep one test on the raw `while not handle.done` loop
  as regression coverage for the still-supported low-level primitive, and
  keep the real-network-flakiness handling from the earlier proposal
  (assert final state hard; only assert internal consistency - non-
  decreasing bytes, correct `total_bytes` - on whatever intermediate
  samples were actually observed, since a fast connection may legitimately
  produce zero of them).

**Do soon** - moderate effort, still Python-only, no native changes:

- `client.py` + `operations/transfer_ops_embedded.py`:
  `start_copy_files()`/`start_delete_files()` and `PartitionedJobHandle`,
  refactoring `copy_files_embedded`/`delete_files_embedded` on top of them.
  Add a `PartitionedJobHandle.watch()` live test once this lands.

**Bigger bet, validate before committing** - touches the independently
versioned `native/rclone` fork:

- A native push callback: `abi.h` gains a callback typedef plus
  `RcloneKitWatchProgress(group, intervalMs, callback, userData,
  *subscriptionId)` / `RcloneKitUnwatchProgress(subscriptionId)`,
  `RCLONEKIT_ABI_VERSION` bumps; `bridge.go` gains a subscription registry
  with one ticker goroutine per subscription re-marshaling the same
  `core/stats` JSON through a new cgo trampoline; `native/abi.py`/
  `native/runtime.py` gain a `ctypes.CFUNCTYPE` registration path with
  careful shutdown ordering in `RcloneRuntime.close()`. Pursue this only
  if a real, measured workload demonstrates 0.5s Python-side polling is
  actually insufficient (e.g. very many small concurrent jobs where
  per-job poll-thread overhead matters) - not speculatively, given the
  cross-repo release cadence and the GIL-safety design work required. If
  built, it should feature-detect via `abi_version` and *supplement*
  `on_progress()` as an internal fast path, never replace the polling-based
  public methods.

## Code style compliance

Per `docs/code_style.md`: `watch()`/`on_progress()`/`ProgressSubscription`
are small, well-named units with docstrings carrying the non-obvious "why"
(GIL/thread-safety rules, additive vs. replacing), not inline comments;
`_DEFAULT_WATCH_INTERVAL_SECONDS` is a named uppercase constant, not a
magic number; the shared `watch()`/`on_progress()` implementation goes
against one narrow `Protocol`, matching the existing `RcJobClient`
pattern, instead of duplicating the same two methods on both `JobHandle`
and `PartitionedJobHandle`.

## Open questions before implementation

1. **Default poll interval for `watch()`/`on_progress()`.** The documented
   manual-loop example uses 5 seconds; `_JobMonitor`'s own internal poll
   interval is 0.5 seconds. What should the new methods default to?
2. **`ProgressSubscription` API shape.** Context manager only, or also a
   bare `.stop()` for callers that don't want to scope it with `with`?
3. **Sequencing.** Confirm the phase order above (watch/on_progress and
   download progress first, partitioned jobs second, native push deferred
   indefinitely pending a real workload need) before any implementation
   starts.
