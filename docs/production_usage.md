# Production usage

This guide describes how to deploy and operate `rclone-kit` as an application
dependency. It focuses on the public API exported from `rclone_kit` and on
resource ownership, failure handling, and verification patterns that matter in
long-running services.

## Supported runtime

`rclone-kit` 1.0.0 requires Python 3.13 or newer. Published wheels support:

- Windows amd64;
- Linux amd64 using the `manylinux2014_x86_64` platform tag.

Each supported wheel contains a native `librclone_kit` shared library, loaded
in-process and verified by checksum against its own manifest at import time.
A wheel installation therefore does not need a system rclone installation,
`PATH` configuration, or a subprocess.

Pin the application dependency in production:

```text
rclone-kit==1.0.0
```

Install only the optional features the process uses:

```bash
pip install "rclone-kit==1.0.0"
pip install "rclone-kit[s3]==1.0.0"
pip install "rclone-kit[database]==1.0.0"
pip install "rclone-kit[postgres]==1.0.0"
pip install "rclone-kit[full]==1.0.0"
```

The `s3` extra adds direct and multipart S3 support. The `database` extra adds
SQLite and database inventory support, while `postgres` also installs the
PostgreSQL driver. `full` installs every optional feature.

The bundled library loads directly from the installed package location; there
is no separate download, cache directory, or install step for it. A writable
home or temp directory is still needed for `Config`'s per-instance temporary
config file and, if used, the mount VFS cache - see the production checklist.

## Configuration

### Mount a configuration file as a secret

For a service, the preferred arrangement is a read-only `rclone.conf` supplied
by the deployment platform:

```python
from pathlib import Path

from rclone_kit import Rclone

CONFIG_PATH = Path("/run/secrets/rclone.conf")

rclone = Rclone(CONFIG_PATH)
```

On Windows, use the corresponding absolute `Path`. Construction fails early
when an explicitly supplied file does not exist.

Alternatively, set `RCLONE_CONFIG` and allow standard discovery:

```python
from rclone_kit import Rclone

rclone = Rclone(None)
```

Discovery checks `RCLONE_CONFIG` and then asks rclone for its active config
path. A warning is emitted when no config can be found, so applications should
treat that warning or a failed startup probe as a deployment error.

### Build configuration in memory

`Config` accepts rclone configuration text or a dictionary:

```python
import os

from rclone_kit import Config, Rclone

config = Config.from_json(
    {
        "archive": {
            "type": "s3",
            "provider": "DigitalOcean",
            "access_key_id": os.environ["OBJECT_STORAGE_ACCESS_KEY"],
            "secret_access_key": os.environ["OBJECT_STORAGE_SECRET_KEY"],
            "endpoint": os.environ["OBJECT_STORAGE_ENDPOINT"],
        }
    }
)
rclone = Rclone(config)
```

When a `Config` object is used, the library materializes it to a private
temporary file at most once per `Config` instance. Do not print the
`Config` or place credentials in source control; the safest production
pattern is a secret-backed config file.

Use rclone's obscured password format when a backend requires it:

```python
obscured_password = rclone.obscure(os.environ["SFTP_PASSWORD"])
```

Obscuring is compatible encoding, not encryption. Protect the resulting value
as a secret.

### Verify configuration during startup

Fail before accepting work if required remotes are absent:

```python
from rclone_kit import Rclone


def verify_storage(client: Rclone, required_remotes: set[str]) -> None:
    configured = {remote.name for remote in client.listremotes()}
    missing = required_remotes - configured
    if missing:
        names = ", ".join(sorted(missing))
        raise RuntimeError(f"Missing rclone remotes: {names}")


verify_storage(rclone, {"archive", "source"})
```

`config_paths()` returns the config, cache, and temporary paths reported by
rclone, in that order. `config_show()` is useful for diagnostics, but its
output can contain secrets; never include it in routine production logs.

### Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `RCLONE_CONFIG` | unset | Config file used when no path is passed to `Rclone`. |
| `RCLONE_KIT_TMP_DIR` | `<os temp dir>/rclone-kit` | Directory the library stages large temporary files in. |
| `RCLONE_KIT_CLEANUP` | `1` | `0` keeps temporary config directories after exit, for debugging. |
| `RCLONE_KIT_VERBOSE` | `0` | `1` enables verbose rclone command logging. |
| `RCLONE_KIT_CHECK` | `1` | `0` disables post-transfer verification by default. |

The library never writes into the current working directory. Byte-range
chunk downloads and S3 multipart upload chunks are staged under
`RCLONE_KIT_TMP_DIR`, which must be writable and large enough to hold the
chunks in flight; point it at a dedicated volume when the process's
temporary filesystem is small or memory-backed. Staged directories are
removed when the operation finishes and, as a fallback, at process exit;
chunk-store files left behind by a killed process are pruned after a day
on the next run.

## Runtime lifecycle and multi-client processes

`Rclone(config)` loads and initializes the native library the first time it's
called with no `runtime=` argument, and owns (closes) that runtime itself.
That default is all a single-config application - a script, a one-shot job,
most CLI tools - needs; skip the rest of this section unless the process
needs more than one `Rclone` client at once.

The native library can be initialized **at most once per process**, not once
per `Rclone` instance or `RcloneRuntime` object: loading the same shared
library twice within one process (e.g. two separate `RcloneRuntime.
from_library_path(...)` calls) returns a handle to the same already-loaded
module and its process-global Go runtime state, so a second, independent
`initialize()` call always fails. A service that wants several `Rclone`
clients - one per request, one per tenant, one per background worker - must
therefore share a single `RcloneRuntime`, constructed exactly once, rather
than let each client build its own:

```python
from pathlib import Path

from rclone_kit import Rclone, shared_runtime

CONFIG_PATH = Path("/run/secrets/rclone.conf")

# Once, at process startup - or lazily on first use; only the first-ever
# call's config_path actually takes effect (see below).
shared_runtime(config_path=CONFIG_PATH)

def handle_request() -> None:
    rclone = Rclone(CONFIG_PATH, runtime=shared_runtime())
    try:
        ...
    finally:
        rclone.close()
```

`shared_runtime()` is a thread-safe, lazy, initialize-once accessor: call it
from anywhere (module import time, a request handler, a worker thread)
without coordinating which caller goes first - every call returns the same
instance, and only the first call's `library_path`/`config_path` arguments
take effect. Each `Rclone` built this way keeps its own job/serve/mount
tracking and can be closed independently without affecting the others or the
shared runtime itself, since `Rclone.close()` only finalizes a runtime it
constructed itself, never one passed in via `runtime=`. Close the shared
runtime only at process shutdown - `RcloneRuntime.close()` is irreversible
for the rest of the process's life; process exit is the only complete
cleanup boundary after that.

Pass the same `rclone_conf` to every `Rclone(...)` call sharing a runtime,
even though only the first-ever call's native initialization actually takes
effect - `self.config` (backing `is_s3()`, `get_s3_credentials()`, and
`encode_fs_spec()`) is always derived from whatever `rclone_conf` a given
`Rclone` instance was constructed with, independently of the runtime's own
state. Passing `None` to a client built against an already-initialized
runtime silently gives that client an *empty* config snapshot - the native
runtime still has the real remotes loaded, but that client's own S3/
credential lookups would not see them.

Because the shared runtime has one immutable config for its whole lifetime,
every client built from it also shares that one underlying config file -
there's no way to give one client a materially different config than another
while they share a runtime. Model different logical tenants as different
remotes within that one shared config (registered dynamically via
`config/create`/`config/update` RC calls if needed), not as separate
runtimes. True hard isolation between clients - a security or tenancy
boundary, not just independent configuration - is not achievable in-process
at all; it requires separate OS processes, each with its own single call to
`shared_runtime()`.

## Authorizing a remote through rclone's own OAuth flow

`Rclone.authorize(...)` drives rclone's non-interactive `config/create`/
`config/update` OAuth state machine and returns an `AuthorizationSession`;
see `docs/rclone_authorization_design.md` for the full design (session
lifecycle, the public callback relay, security requirements). Two points
follow directly from this section's runtime-sharing model and are easy to
miss:

- **The session queue is per `RcloneRuntime`, not per `Rclone` client.**
  `AuthorizationManager.for_runtime()` resolves to the same manager for
  every client sharing a runtime (mirroring `shared_runtime()`'s own
  "construct exactly once, share everywhere" pattern), because rclone's own
  OAuth flow state is a process-wide Go global - at most one authorization
  may be driving it at a time, across every client on that runtime, not just
  within one client.
- **A client's `self.config` snapshot does not update itself.** Same
  caveat as above: a remote an authorization session creates or updates is
  written straight into the runtime's one shared config file the moment it
  succeeds, but an existing `Rclone` instance's own `self.config` snapshot
  (backing `is_s3()`, `get_s3_credentials()`, `encode_fs_spec()`) was
  captured at that instance's construction time and will not see it. If you
  need those methods to see a remote a session just authorized, construct a
  fresh `Rclone(rclone_conf, runtime=shared_runtime())` afterward rather than
  assuming an existing client picks it up.

## Paths and result objects

Remote paths use rclone syntax:

```text
remote:bucket/prefix/file.ext
```

Local paths use normal operating-system paths. Do not invent a `local:`
remote unless one is actually defined in the rclone config.

Short-lived operations return `OperationResult` where the command result is
useful:

```python
result = rclone.copy(
    "/srv/incoming",
    "archive:training-data/incoming",
    check=True,
)
if not result.ok:
    raise RuntimeError(result.error)
```

Set `check=True` on copy operations when command failure should immediately
raise. `copy_files()` partitions its file list internally but still folds
every partition into one result:

```python
result = rclone.copy_files(
    src="source:dataset",
    dst="archive:dataset",
    files=["images/0001.png", "labels/0001.json"],
    check=True,
)
if not result.ok:
    raise RuntimeError("One or more copy partitions failed")
```

## Listing and metadata

### List one directory

`ls()` returns a `DirListing` with separate `dirs` and `files` collections:

```python
from rclone_kit import ListingOption, Order

listing = rclone.ls(
    "archive:training-data/images",
    max_depth=0,
    glob="*.png",
    order=Order.NORMAL,
    listing_option=ListingOption.FILES_ONLY,
)

for file in listing.files:
    print(file.name, file.size, file.mod_time_dt())
```

`max_depth=0` lists the immediate path. A negative depth requests recursive
listing. `glob` is applied to returned paths by the Python client.

Other metadata helpers cover common probes:

```python
from rclone_kit import SizeSuffix

path = "archive:training-data/manifest.json"

if rclone.exists(path):
    modified_at = rclone.modtime_dt(path)
    size: SizeSuffix = rclone.size_file(path)
    print(modified_at, size.as_int())
```

`stat()` and `size_file()` raise `FileNotFoundError` for a missing file.
`size_file()` also raises `ValueError` when the path matches more than one
file.

For a selected group, `size_files()` returns the aggregate and individual
sizes without listing unrelated objects:

```python
result = rclone.size_files(
    src="archive:training-data",
    files=["images/0001.png", "labels/0001.json"],
    check=True,
)

print(result.total_size)
for relative_path, size in result.file_sizes.items():
    print(relative_path, size)
```

File names passed to `size_files()` are relative to `src`.

### Stream very large inventories

Use `ls_stream()` when a recursive inventory should not be materialized in
memory:

```python
with rclone.ls_stream(
    "archive:training-data",
    max_depth=-1,
    fast_list=False,
) as stream:
    for page in stream.files_paged(page_size=10_000):
        persist_inventory_page(page)
```

Always use the context manager so the underlying process is terminated and
its temporary configuration is removed if iteration stops early.

`fast_list=True` reduces backend transactions on remotes where rclone
supports it, but can consume much more memory because rclone loads a full
recursive listing. Enable it only after measuring the target backend and
inventory size.

### Walk directory by directory

`walk()` yields one `DirListing` per visited directory:

```python
for directory in rclone.walk(
    "archive:training-data",
    max_depth=2,
    breadth_first=True,
):
    for file in directory.files:
        print(file.to_string())
```

Set `breadth_first=False` for depth-first traversal. Use
`scan_missing_folders()` when only missing directory structure matters:

```python
for missing_dir in rclone.scan_missing_folders(
    src="source:dataset",
    dst="archive:dataset",
):
    print(missing_dir)
```

When comparing source and destination, make their roots semantically
equivalent. An off-by-one parent directory produces misleading differences.

## Reading and writing objects

Small control files can be handled directly:

```python
import json

manifest_path = "archive:jobs/run-42/manifest.json"

rclone.write_text(
    json.dumps({"run_id": 42, "state": "ready"}),
    manifest_path,
)
manifest = json.loads(rclone.read_text(manifest_path))
```

`read_bytes()` and `write_bytes()` are convenient for small payloads, but
they buffer the complete object in memory and use a temporary local file.
Use transfer methods or HTTP downloads for large data.

Read a byte range directly to disk:

```python
from pathlib import Path

from rclone_kit import SizeSuffix

rclone.copy_bytes(
    src="archive:models/model.bin",
    offset=SizeSuffix("64M"),
    length=SizeSuffix("8M"),
    outfile=Path("/srv/work/model.part"),
)
```

`SizeSuffix` accepts values such as `16K`, `1.5M`, and `2G`, and can be
converted with `as_int()` or `as_str()`.

## Transfer workflows

### Copy a tree or one file

`copy()` uses tuned defaults for recursive transfers and does not delete
objects that exist only at the destination:

```python
result = rclone.copy(
    src="/srv/incoming/run-42",
    dst="archive:runs/run-42",
    check=True,
    transfers=32,
    checkers=256,
    low_level_retries=10,
    retries=3,
)
assert result.ok
```

Use `copy_to()` when both source and destination name one file:

```python
result = rclone.copy_to(
    "/srv/incoming/manifest.json",
    "archive:runs/run-42/manifest.json",
    check=True,
)
assert result.ok
```

### Mirror a tree with `sync()`

`sync()` makes the destination identical to the source. It is destructive
on the destination: every file under `dst` with no matching source path is
deleted. There is no undo. Keep it behind application-level authorization
and verify path construction before enabling it in a worker.

```python
result = rclone.sync(
    src="/srv/incoming/run-42",
    dst="archive:runs/run-42",
    check=True,
    transfers=32,
    checkers=256,
)
assert result.ok
```

`sync()` runs the underlying sync **exactly once**. `copy()` uses the
fork's own retry-aware `rclonekit/copy` RC method, which wraps the copy in
the same high-level retry loop the `rclone copy` command uses and reports
each attempt in `OperationResult.attempts`. There is no `rclonekit/sync`
equivalent, so `sync()` calls upstream `sync/sync` directly: nothing is
retried at the command level, a transient failure is final, and
`result.attempts` is always empty. For that reason `sync()` takes no
`retries` parameter at all - `_config.Retries` is read only by the retry
loop it does not have. The loop is deliberately not reimplemented in
Python: rclone resets its accounting group's error state between attempts,
which an out-of-process caller cannot do correctly, and a naive retry
would double-count stats. Per-file `low_level_retries` still applies.

Retry a sync at the application level if you need one, and treat each
attempt as a fresh operation:

```python
for _attempt in range(3):
    result = rclone.sync(source, destination, check=False)
    if result.ok:
        break
    time.sleep(5)
```

### Move a tree or one file

`move()` transfers and then removes the source. It is destructive on the
source, not on the destination - files that exist only at `dst` survive,
which is what separates it from `sync()`. `delete_empty_src_dirs=True`
also removes the emptied source directories.

```python
result = rclone.move(
    src="source:staging/run-42",
    dst="archive:runs/run-42",
    check=True,
    delete_empty_src_dirs=True,
)
assert result.ok
```

`move_to()` is the single-file counterpart of `copy_to()`:

```python
result = rclone.move_to(
    "source:staging/manifest.json",
    "archive:runs/run-42/manifest.json",
    check=True,
)
assert result.ok
```

`move()` carries the same no-command-level-retry caveat as `sync()` (it
calls upstream `sync/move`) and likewise takes no `retries` parameter. A
failure part-way leaves some files moved and the rest still at the source.
rclone moves server-side where the backend supports it and falls back to
copy-then-delete where it does not, so an interrupted single-file
`move_to()` can leave the file on both sides - never on neither.

### Copy a selected file set

Names are relative to the supplied source root and must not include a remote
prefix:

```python
selected = [
    "images/0001.png",
    "images/0002.png",
    "labels/index.json",
]

results = rclone.copy_files(
    src="source:dataset",
    dst="archive:dataset",
    files=selected,
    check=True,
    max_partition_workers=4,
    transfers=16,
    checkers=128,
    retries=3,
    retries_sleep="5s",
    timeout="10m",
)
```

`files` may also be a `Path` to a newline-delimited file list.
`max_partition_workers` creates independent rclone commands grouped by common
prefix; it multiplies total transfer concurrency, so tune it together with
`transfers`.

### Verify before cleanup

A conservative move workflow copies, verifies, and only then deletes:

```python
source = "source:completed/run-42"
destination = "archive:completed/run-42"

copy_result = rclone.copy(source, destination, check=True)
if not copy_result.ok:
    raise RuntimeError(copy_result.error)

if not rclone.is_synced(source, destination):
    raise RuntimeError("Destination verification failed")

purge_result = rclone.purge(source)
if not purge_result.ok:
    raise RuntimeError(purge_result.error)
```

`purge()` is destructive: it removes the path and all contents. Keep it
behind application-level authorization and test path construction before
enabling it in a production worker.

`is_synced()` answers only yes/no. Use `check()` when the worker needs to
act on *which* paths disagree; it returns a frozen `CheckResult` and never
raises just because the two sides differ:

```python
report = rclone.check(source, destination, match=True)
if not report.success:
    raise RuntimeError(
        f"{report.status}: "
        f"{len(report.differ)} differ, "
        f"{len(report.missing_on_dst)} missing at the destination"
    )
```

`CheckResult` fields: `success` (bool), `status` (rclone's textual summary,
`"OK"` on success), `hash_type` (`None` for a download-based check), and
the tuples `combined`, `missing_on_src`, `missing_on_dst`, `match`,
`differ`, `error`. An array rclone did not report is an empty tuple, and
each report flag left unset uses rclone's own default: `combined` and
`match` off; `missing_on_src`, `missing_on_dst`, `differ`, and `error` on.

`check()` alters neither side. `one_way=True` checks only that every source
file exists at the destination; `download=True` compares contents instead
of hashes, for backends with no usable hash; `size_only`, `checkers`, and
`fast_list` tune the comparison itself.

Unlike `copy()`/`sync()`, `check()` is a direct synchronous RC call with no
`JobHandle` - it cannot be cancelled or progress-polled, because the report
is the call's return value and rclone reports nothing until the comparison
finishes. It does not block anything else: the embedded runtime holds no
lock for the duration of a call, so only the calling thread waits.

For selected files, `delete_files()` accepts one path or a list of fully
qualified remote paths:

```python
result = rclone.delete_files(
    [
        "source:dataset/stale/0001.bin",
        "source:dataset/stale/0002.bin",
    ],
    check=True,
    rmdirs=True,
)
if not result.ok:
    raise RuntimeError(result.error)
```

## Asynchronous copies, partitioned operations, and result types

The client links the bundled native library directly in-process; there is no `rclone` executable and
no subprocess per call. `copy()`, `copy_to()`, `copy_dir()`, `copy_remote()`, `sync()`, `move()`,
`move_to()`, `purge()`, `cleanup()`, `copy_files()`, and `delete_files()` all return `OperationResult`
(`size_files()` returns `SizeResult`; `check()` returns `CheckResult`), and `check=True` still raises on
failure - code that only inspects `.ok`/`.error` needs no special handling. `start_copy()`,
`start_sync()`, and `start_move()` give direct access to the underlying asynchronous job model when a
caller needs more than a blocking call.

### Real asynchronous control with `start_copy()`/`start_sync()`/`start_move()`

`copy()`/`copy_dir()`/`copy_remote()`/`sync()`/`move()` all block internally: they start the transfer as
an embedded job and immediately wait for it. Call `start_copy()` (or `start_sync()`/`start_move()`,
which take the same tuning parameters minus `retries`, plus `sync/move`'s `delete_empty_src_dirs`)
directly when the caller needs to observe progress, apply a bounded wait, or cancel a transfer already
in flight:

```python
handle = rclone.start_copy(
    "/srv/incoming/run-42",
    "archive:runs/run-42",
    transfers=32,
    checkers=256,
    low_level_retries=10,
    retries=3,
)

try:
    result = handle.wait(timeout=3600)
except OperationTimeoutError:
    # The deadline only bounds observation - the transfer is still running.
    # Cancel explicitly if it should stop.
    handle.cancel()
    result = handle.wait(timeout=60)

if not result.ok:
    raise RuntimeError(result.error)
```

All three return the same `JobHandle`, but only a copy's `OperationResult.attempts` is ever populated:
`start_sync()`/`start_move()` run upstream `sync/sync`/`sync/move`, which have no command-level retry
loop to report attempts from. See "Mirror a tree with `sync()`" above.

`wait(timeout=...)` never cancels the underlying operation by itself - a timeout means "we stopped
watching," not "it stopped running." Call `cancel()` for that; it returns immediately (`True` if the
cancellation request was accepted, `False` if the job had already settled) and never blocks, so pair it
with a bounded `wait()` to observe confirmed termination. `JobHandle` is also a context manager: exiting
a `with` block cancels an unfinished owned job and waits up to a bounded interval, without raising on
timeout, so the surrounding job-queue/worker layer decides what an unresponsive job means rather than
`JobHandle` deciding for it.

Poll progress with `status()`/`stats()` while a transfer runs:

```python
while not handle.done:
    stats = handle.stats()
    print(f"{stats.bytes}/{stats.total_bytes} bytes, {stats.transfers} files")
    time.sleep(5)
```

This manual loop is not deprecated - it is still the right tool for a caller that wants to interleave
stats checks with other work on its own thread (e.g. inside a UI event loop) rather than receive a
callback on a background thread. For most callers, though, `watch()`/`on_progress()` below are the
recommended idiom: they encapsulate the same loop and remove the two easy ways to get it subtly wrong
(calling `.stats()` again after `.done` already flipped true, or picking too tight a poll interval).

### `watch()`/`on_progress()`: the recommended progress idiom

`watch()` yields a `TransferStats` snapshot on the calling thread every `interval` seconds until the job
settles, with the final snapshot always yielded last:

```python
handle = rclone.start_copy(source, destination)
for stats in handle.watch(interval=5):
    print(f"{stats.bytes}/{stats.total_bytes} bytes, {stats.transfers} files")
result = handle.wait()
```

`on_progress()` runs a callback on its own dedicated background thread instead, so progress reporting
never blocks the caller's thread and one job's slow callback can never delay another job's status
polling (each subscription gets its own thread; none of them share `JobHandle`'s internal poll thread).
It returns a `ProgressSubscription` that is also a context manager:

```python
def report(stats):
    print(f"{stats.bytes}/{stats.total_bytes} bytes")

handle = rclone.start_copy(source, destination)
with handle.on_progress(report, interval=5):
    result = handle.wait()
```

A callback exception is logged and swallowed rather than crashing the subscription thread. Both methods
are defined once against a narrow `ProgressSource` Protocol and work identically on the
`PartitionedJobHandle` `start_copy_files()`/`start_delete_files()` return - see below.

### `copy_files()`/`delete_files()`: partitioned operations under one result

`copy_files()` and `delete_files()` partition their file list by common directory/remote prefix, so
unrelated transfers run concurrently, but every partition folds into one `OperationResult` before
returning - not one per partition. A partial failure never aborts collecting the rest: every
partition runs to completion first, and only then does `check=True` raise (once, for the aggregate)
if any partition failed.

```python
result = rclone.copy_files(source, destination, ["a.txt", "b/c.txt"], check=False)
if not result.ok:
    for warning in result.warnings:
        print(warning.message)  # "<partition src> -> <partition dst>: <error>", one per failure
```

`copy_files()` and `delete_files()` each return a single `OperationResult` whose `job_ids`/`attempts`/
`stats` span every partition. A file entry that does not exist is not an error in either operation - it
is simply not visited during the underlying walk.

`start_copy_files()`/`start_delete_files()` mirror `start_copy()`/`copy()`: they start every partition
job immediately and return a non-blocking `PartitionedJobHandle` instead of waiting.
`PartitionedJobHandle` exposes the same `.done`/`.stats()`/`.watch()`/`.on_progress()`/`.wait()`/
`.cancel()` surface as `JobHandle`, aggregated across every partition:

```python
with rclone.start_copy_files(source, destination, ["a.txt", "b/c.txt"]) as handle:
    for stats in handle.watch(interval=5):
        print(f"{stats.bytes}/{stats.total_bytes} bytes across all partitions")
    result = handle.wait()
```

Unlike the blocking wrappers, neither non-blocking entry point supports `max_partition_workers` pacing -
a fire-and-forget start has no notion of "outstanding" jobs to throttle - and `start_delete_files()` does
not support `rmdirs=True`: that step only starts once a given partition's delete has been observed to
succeed, which has no non-blocking equivalent without a background orchestrator. Use the blocking
`delete_files(rmdirs=True)` for that case.

### `ls_stream()`/`copy_bytes()`: bounded-memory streaming and byte ranges

`ls_stream()` is a context manager exposing `.files()`/`.files_paged()`:

```python
with rclone.ls_stream("archive:training-data", max_depth=-1) as stream:
    for page in stream.files_paged(page_size=10_000):
        persist_inventory_page(page)
```

It pulls items in small batches from a bounded server-side buffer, so memory stays bounded regardless
of how many millions of entries the listing has. `save_to_db()` needs no separate handling either,
since it only ever calls `ls_stream()`. Always use the context manager (as above): exiting it -
including via an exception partway through iteration - releases the underlying stream immediately
rather than leaving it open for the life of the runtime.

`copy_bytes()` extends past the end of the object without error - it copies whatever is available.
`check` is not exposed on it: a failure always raises `RcloneCommandError`.

## Streaming differences and reconciliation

`diff()` streams comparison results while rclone is still running:

```python
from rclone_kit import DiffOption, DiffType

missing = rclone.diff(
    src="source:dataset",
    dst="archive:dataset",
    diff_option=DiffOption.MISSING_ON_DST,
    fast_list=True,
    size_only=True,
    checkers=256,
)

for item in missing:
    assert item.type is DiffType.MISSING_ON_DST
    enqueue_copy(item.src_path(), item.dst_path())
```

Use `DiffOption.COMBINED` to receive equal, missing, different, and error
records in one stream. `min_size` and `max_size` accept rclone size strings
such as `"10M"`. The default `fast_list=True` is optimized for full-tree
comparisons; disable it if its memory use is unsuitable for the inventory.

## Filesystem-style remote access

`RemoteFS` and `FSPath` offer a small `pathlib`-like interface. Constructing one binds no port and
starts no server - `exists()`/`is_dir()`/`is_file()`/`ls()` go straight through RC (`operations/stat`/
`operations/list`), and `read_bytes()`/`write_binary()`/`copy()`/`remove()` go through the same
`copy_to()`/`read_bytes()`/`write_bytes()`/`delete_files()` methods described above:

```python
with rclone.filesystem("archive:jobs") as remote_fs:
    root = remote_fs.cwd()
    job = root / "run-42"
    manifest = job / "manifest.json"

    if manifest.exists():
        print(manifest.read_text())

    output = job / "worker-result.json"
    output.write_text('{"status":"complete"}')

    with job.walk_begin(max_backlog=8) as walker:
        for current, dirnames, filenames in walker:
            print(current, dirnames, filenames)
```

Scope `RemoteFS` with `with` regardless - it may still hold other resources (and its `dispose()` must
run for those) even though the common path starts no server. `FSPath.write_bytes()` buffers its
input, and remote `mkdir()` is not supported because object stores usually represent directories as
prefixes. Call `remote_fs.serve(addr=...)` explicitly if some other code genuinely needs a real
`HttpServer` (e.g. multipart/resumable transfers) - it is never started implicitly.

Local paths can use the same interface without launching rclone:

```python
from pathlib import Path

from rclone_kit import FSPath

local = FSPath.from_path(Path("/srv/work"))
for current, dirnames, filenames in local.walk():
    print(current, dirnames, filenames)
```

## Scoped HTTP downloads

`serve_http()` is useful for repeated reads, parallel downloads, and byte
ranges:

```python
from pathlib import Path

from rclone_kit import Range, SizeSuffix

with rclone.serve_http("archive:models") as server:
    remote_name = "releases/model-v4.bin"
    print(server.size(remote_name))

    server.download_multi_threaded(
        src_path=remote_name,
        dst_path=Path("/srv/models/model-v4.bin"),
        chunk_size=SizeSuffix("32M").as_int(),
        n_threads=8,
        on_progress=lambda done, total: print(f"{done}/{total} bytes"),
    )

    header = server.get(
        remote_name,
        range=Range(start=0, end=SizeSuffix("4K")),
    )
```

`on_progress`, when given, is called once per completed chunk (not a smooth per-byte stream, since each
chunk is fetched as one blocking HTTP request) with the running byte total and the overall size. This
has no relationship to the `JobHandle`/`PartitionedJobHandle` progress model above - it is plain chunked
HTTP I/O, not an rclone RC job.

The `Range` end is exclusive. The server context manager shuts down the
server even if a download raises. Bind to the automatically selected
localhost port unless another process must reach the endpoint. If a fixed
address is required, restrict it with host firewall and deployment network
policy. The returned `HttpServer` always talks to the running server over
real HTTP.

## Mounts and WebDAV

Mounts require FUSE on Linux or WinFsp on Windows. Use a context manager so
unmounting and optional cache cleanup happen on every exit path:

```python
from pathlib import Path

mount_path = Path("/mnt/archive")

with rclone.mount(
    src="archive:datasets",
    outdir=mount_path,
    allow_writes=False,
    vfs_cache_mode="full",
    transfers=32,
) as mounted:
    consume_files(mounted.mount_path)
```

For object storage, `mount_s3()` supplies S3-oriented VFS defaults:

```python
with rclone.mount_s3(
    url="archive:datasets",
    outdir=Path("/mnt/archive"),
    allow_writes=False,
    vfs_cache_mode="full",
    vfs_disk_space_total_size="20G",
) as mounted:
    consume_files(mounted.mount_path)
```

Mounts are operational infrastructure: provision disk for the VFS cache,
monitor its utilization, and run mount-specific smoke tests on the target OS.

`mount()`/`mount_s3()` dispatch to rclone's own `mount/mount` RC method and
return a `MountHandle`, whose `mount_path`, idempotent `.dispose()`, and
context-manager close are used the same way above. This requires the native
library to have been built with `scripts/native/build.py --profile
production` (Windows: WinFsp's SDK installed and on the build machine's
`CPATH`; Linux: needs no build-time FUSE headers, only the `fuse3`/`fuse`
runtime package on the machine that actually mounts - see
`native/README.md`). Verified against a real mount on Windows; Linux mount
support compiles in the same way but has not yet been exercised end-to-end
against a real mount in this repo. A
`--profile development` build (this project's default) has no real mount
implementation compiled in at all, and calling `mount()`/`mount_s3()`
against one raises a plain `RcCallError`, not a confusing crash. The VFS
cache directory itself is not caller-configurable through this API; rclone
manages it at its own default location.

`serve_webdav()` returns a long-lived handle. Bind it to a private interface,
require credentials, and scope it:

```python
with rclone.serve_webdav(
    src="archive:shared",
    user="service-user",
    password=webdav_password,
    addr="127.0.0.1:9080",
) as handle:
    run_consumer()
```

The handle is a `ServeHandle`, supporting the context manager and idempotent
`.dispose()`.

## S3-optimized operations

Install the `s3` extra before using these methods.

For a local file below the backend's normal multipart threshold, upload
directly with the S3 client:

```python
from pathlib import Path

rclone.copy_file_s3(
    src=Path("/srv/out/model.bin"),
    dst="archive:models/model.bin",
)
```

The destination must include remote, bucket, and key. The configured remote
must have type `s3` or `b2`.

For a large remote-to-S3 copy that must resume after interruption, split the
source into explicit parts:

```python
from rclone_kit import PartInfo, SizeSuffix

source = "source:exports/full.tar"
destination = "archive:exports/full.tar"
source_size = rclone.size_file(source)
parts = PartInfo.split_parts(
    size=source_size,
    target_chunk_size=SizeSuffix("128M"),
)

rclone.copy_file_s3_resumable(
    src=source,
    dst=destination,
    part_infos=parts,
    upload_threads=8,
    merge_threads=4,
)
```

The operation stores resumable state and temporary objects beside the
destination using a `-parts` suffix. A later call with the same source,
destination, and part layout can resume completed work. Keep the source
immutable for the duration of the operation, choose a stable part size, and
do not purge the parts prefix while a retry may occur.

## Database inventory

Install `database` for SQLite or the appropriate database extra for the
server driver:

```python
rclone.save_to_db(
    src="archive:training-data",
    db_url="sqlite:///inventory.db",
    max_depth=-1,
    fast_list=False,
)
```

For PostgreSQL:

```python
import os

rclone.save_to_db(
    src="archive:training-data",
    db_url=os.environ["INVENTORY_DATABASE_URL"],
    max_depth=-1,
)
```

Pass the root-most path that should become one inventory table. The client
streams the listing in pages rather than loading it all in memory. Keep
database URLs out of logs because they may contain credentials.

## Build identification

There is no separate `rclone` executable to check the version of - the client already has direct
in-process RC access, and the native library ships with the package. Query `Rclone.native_build_info()`
for "which rclone build am I actually running" information (useful in health checks and support
diagnostics):

```python
info = rclone.native_build_info()
print(info.rclone_version, info.rclone_commit, info.go_version, info.target)
```

## Logging and error handling

The library does not configure the root logger during import. Integrate it
with the application's logging policy:

```python
import logging

from rclone_kit import LogSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("rclone_kit").setLevel(logging.INFO)

LogSettings.rclone_verbose(True)
LogSettings.enable_upload_parts_logging(True)
```

Verbose logging is useful during rollout but can be noisy. The application
remains responsible for not logging config contents or database URLs.

Handle the typed library errors at the boundary where retry or alert policy
is decided:

```python
from rclone_kit import HttpFetchError, MissingOptionalDependencyError, RcloneKitError

try:
    run_storage_job(rclone)
except MissingOptionalDependencyError:
    # Deployment packaging error: fail permanently and alert.
    raise
except HttpFetchError as error:
    # Network or remote HTTP error: apply the job's bounded retry policy.
    schedule_retry(error)
except RcloneKitError as error:
    mark_job_failed(error)
```

`RcloneKitError` is the root of the entire library hierarchy, so the last
clause is a genuine catch-all: the per-subsystem base types (`RcCallError`
for a failed RC call, `NativeError` for an ABI-level fault, and
`RcloneRuntimeError` for a platform/download/cache fault) all subclass it.
`MissingOptionalDependencyError` is the one deliberate exception — it
subclasses `ImportError`, because a missing extra is a deployment
packaging fault rather than a storage operation failing, and belongs in a
permanent-failure branch ahead of the catch-all rather than inside it.

Every exception type in that hierarchy is importable directly from
`rclone_kit`. The defining modules (`rclone_kit.exceptions`,
`rclone_kit.rc.errors`, `rclone_kit.native.errors`) remain available, but
only the package root carries a compatibility promise.

`FileNotFoundError` is used for missing local or remote targets in several
filesystem and metadata operations. `ValueError` generally means invalid or
ambiguous input and should not be retried unchanged.

## Console scripts

The installed command-line adapters are useful for scheduled jobs and
operations tooling:

```bash
rclone-kit-listfiles --config /run/secrets/rclone.conf archive:dataset
rclone-kit-save-to-db --config /run/secrets/rclone.conf \
  --db sqlite:///inventory.db archive:dataset
rclone-kit-copylarge-s3 --config /run/secrets/rclone.conf \
  source:exports/full.tar archive:exports/full.tar
```

Run each command with `--help` before automation. Prefer the Python API when
the caller needs structured results, custom retry policy, or composition with
application transactions.

## Production checklist

Before rollout:

- pin the `rclone-kit` version and the required extras;
- deploy on a certified OS and architecture with Python 3.13 or newer;
- provide a writable home or temp directory for `Config`'s temporary config
  file, and enough VFS or temporary disk space if mounting;
- size the staging area for concurrent byte-range and multipart chunks, and
  set `RCLONE_KIT_TMP_DIR` when the default temporary filesystem is too
  small or is memory-backed;
- mount `rclone.conf` read-only from secret storage;
- validate required remotes and a representative read during startup;
- set explicit transfer, checker, partition-worker, and HTTP thread limits;
- use context managers for `FilesStream`, `RemoteFS`, `HttpServer`,
  `MountHandle`, and `ServeHandle`;
- make source data immutable during multipart and verification workflows;
- copy and verify before any purge or delete;
- bound retries at both rclone and job-queue layers to avoid retry storms;
- keep config text, database URLs, and credentials out of logs;
- monitor command duration, failure count, bytes transferred, cache space,
  and orphaned long-lived processes;
- exercise cloud, mount, and large-object smoke tests against the actual
  production backend before enabling destructive workflows.
