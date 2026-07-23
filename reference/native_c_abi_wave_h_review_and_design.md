# Native C ABI migration: Wave H design (serve and mount resources)

Status: partially done - R03/R04/R05 complete; R01/R02 (`mount()`/`mount_s3()`) genuinely blocked
on a production build toolchain gap, not attempted

Date: 2026-07-24

Pinned native source: `native/rclone` at `a5866fdbe` on `rclone-kit/integration-v1` (local-only per
the standing constraint; not pushed to the fork remote without separate authorization)

Related documents:

- [Wave D review and design](native_c_abi_wave_d_review_and_design.md)
- [Wave G review and design](native_c_abi_wave_g_review_and_design.md)
- [CLI-to-C-ABI migration plan and ledger](rclone_cli_to_c_abi_migration_plan.md)

## 1. Scope

Ledger rows R01 (`mount()`), R02 (`mount_s3()`), R03 (`serve_webdav()`), R04 (`serve_http()`), R05
(explicit server/mount listing and cleanup). D12/D13 are, as with D10/D11 (Wave F) and D14/D15
(Wave G), this migration plan's own internal/distribution-removal ledger, not action items here.

## 2. What was investigated before writing any Python code

The bridge's own `imports.go` comment already stated the gate for this wave: "Mount and serve
implementations are added here only once their RC handles, typed lifecycle, and platform build
tags are implemented and tested." Before assuming that gate still applied unchanged, three things
were checked directly against the pinned source and the real built DLL:

- **`cmd/serve/http` and `cmd/serve/webdav`** (the actual protocol implementations `serve/start`
  dispatches to) import `github.com/rclone/rclone/cmd` (the shared root/helper package every leaf
  command needs) but **not** `cmd/all` - the aggregator the bridge deliberately avoids. Added both
  to `imports.go`, rebuilt, and it compiled cleanly with no new build tags.
- **A real `serve/start type=http` → HTTP GET → `serve/list` → `serve/stop` round trip**, and the
  same for `type=webdav`, were run against the freshly built DLL before writing any Python wiring.
  Both worked exactly as documented in `cmd/serve/rc.go`'s own RC help text.
- **`cmd/mountlib`** (the `mount/*` RC method registry) has no dependency on `cmd`, FUSE, or WinFsp
  at all - only `fs`, `fs/rc`, `vfs/vfscommon`. Added it alone (no `cmd/mount`/`cmd/cmount`, which
  need the actual platform driver toolchain) and verified empirically: `mount/listmounts` returns
  `{"mountPoints": []}` and `mount/unmountall` succeeds trivially (nothing registered to unmount),
  while `mount/mount` correctly fails with `"mount option specified is not registered, or is
  invalid"` - an honest, typed RC error, not a crash or a silent no-op.

This confirmed R03/R04/R05 need no platform toolchain work at all and could be completed this wave;
R01/R02 genuinely cannot be, until FUSE (Linux) or WinFsp (Windows) production-profile build tags
exist (`scripts/native/build.py --profile` already documents this as not yet implemented).

## 3. Design decisions

### H1 - `rc/serve.py`: a third RC boundary module, mirroring `rc/jobs.py`/`rc/list_stream.py`

`serve/start`/`serve/stop`/`serve/list` are plain, already-synchronous RC calls - no bounded-buffer
cursor (unlike L02) and no async-job routing (unlike T01/T02/T07/T13) is needed, since starting a
server returns immediately once the listener is bound. `RcloneRcServeClient` wraps them with the
same strict-parsing discipline as `rc/jobs.py`/`rc/list_stream.py`: `ServeRef(id, addr)` is built
from the wire response, never assumed from what was requested - `addr` in particular is always the
*actual* bound address (a requested port `0` resolves to whatever ephemeral port rclone actually
bound), avoiding the inherent TOCTOU race a Python-side `find_free_port()` step would have.

### H2 - `ServeHandle`: deliberately minimal, matching what callers already used

`serve_webdav()`'s historical CLI return type is a bare `Process` - callers only ever kept it alive
and eventually shut it down, never used `Process`-specific behavior (no consumer reads `.stdout`,
`.poll()`, etc. from a webdav-serve `Process`, confirmed by search). `ServeHandle` matches that same
minimal shape: `id`, `addr`, idempotent `dispose()`, context manager. `serve_webdav()`'s embedded
path returns a `ServeHandle` directly; `serve_http()` wraps one inside `HttpServer` (H3).

### H3 - `HttpServer` needed zero behavioral changes, only a widened type

Reading `HttpServer` before touching it showed `self.process` is used for exactly two things:
`is None` (alive-check) and `.dispose()` (teardown) - never any `Process`-specific attribute or
method. So `HttpServer.__init__`'s `process` parameter was retyped from the concrete `Process` class
to a two-line structural `Protocol` (`_DisposableServerHandle`, `dispose() -> None`) that both
`Process` and `ServeHandle` already satisfy. Every other line of `HttpServer` - the `httpx`-based
`get`/`put`/`delete`/`list`/`download`/`download_multi_threaded` methods - is completely unchanged
and works identically over an embedded-started server, since it always talks to the server over
real HTTP regardless of who started the listening process.

### H4 - `serve_http()`/`serve_webdav()` reuse the CLI backend's exact flag defaults

`fetch_serve_http_embedded()` sends the same `vfs_disk_space_total_size=0`/
`vfs_read_chunk_size_limit=512M` defaults `launch_http_server()`'s CLI path hard-codes, using RC's
own `--foo-bar` → `foo_bar` parameter-naming convention (documented in `serve/start`'s own RC help
text). `fetch_serve_webdav_embedded()` sends `user`/`pass`/optional `allow_other` the same way.
Neither exposes `other_args` under `execution="embedded"` - both raise
`UnsupportedEmbeddedOperationError` if it is nonempty, matching every other embedded row's contract.

### H5 - R05: the runtime tracks handles it started, disposed idempotently at `close()`

`Rclone` gained `self._serve_handles: set[ServeHandle]`, populated whenever `serve_http()`/
`serve_webdav()` starts one under embedded execution. `close()` disposes every tracked handle before
tearing down the job monitor and (if owned) the runtime itself - mirroring the exact pattern already
established for jobs (`_JobMonitor.shutdown()`) in Wave D. No separate "did the caller already
dispose this" bookkeeping was needed: `ServeHandle.dispose()` is already idempotent, so `close()` can
unconditionally dispose every tracked handle and a caller's own earlier `dispose()` call just makes
that a no-op.

### H6 - Pre-existing "NFS" wording bug fixed while touching this code

The ledger's own R03 note ("Existing NFS wording is corrected") named a real, pre-existing
docstring/error-message bug: `launch_webdav_server()` and `Rclone.serve_webdav()` both described
serving WebDAV as "via NFS" (a copy-paste error from an unrelated protocol name, unrelated to this
migration otherwise). Fixed in both places, plus the one test asserting on the old wrong error text.

## 4. R01/R02 (`mount()`/`mount_s3()`): not attempted, not silently skipped either

Confirmed empirically (section 2) that `mount/mount` is already wired to a real, typed RC error
today - calling it under `execution="embedded"` would surface a clear `RcCallError`
("mount option specified is not registered, or is invalid"), not a crash or silent fallback. Porting
`mount()`/`mount_s3()` themselves needs a real platform mount implementation (`cmd/mount` on
Linux/FUSE, `cmd/cmount` with WinFsp on Windows) compiled into the production build profile, which
does not exist yet (`scripts/native/build.py --profile` only implements `development`, with no-mount
build tags, today). Building that toolchain - a new production build profile, WinFsp/FUSE headers
and linking, a privileged-mount test harness - is a distinct, substantial undertaking from this
wave's RC-wiring work and was not attempted without that groundwork existing first.

## 5. Test coverage plan

Unit tests (fake `RcServeClient`) cover `rc/serve.py`'s wire mapping, `ServeHandle`'s dispose
idempotency, `serve_ops_embedded.py`'s parameter mapping (default VFS flags, cache mode, webdav
credentials, `allow_other`), and `Rclone`'s dispatch/close-tracking behavior via a fake `NativeBinding`.
Native-DLL parity tests cover `serve_http()` (CLI-vs-embedded `get`/`list` parity, `shutdown()`
disposing the handle, `other_args` rejection) and `serve_webdav()` (start/stop, `close()` disposing an
un-disposed handle) against real local directories.
