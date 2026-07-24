# Native C ABI migration: Wave H design (serve and mount resources)

Status: complete - R01-R05 all done (mount addendum below closes the R01/R02 gap this document
originally left open)

Date: 2026-07-24 (mount addendum: 2026-07-24)

Pinned native source: `native/rclone` at `a5866fdbe` on `rclone-kit/integration-v1` for R03-R05;
`ff2f143a4` (adds `cmd/cmount` to the bridge) for the R01/R02 mount addendum (local-only per the
standing constraint; not pushed to the fork remote without separate authorization)

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

**Update (mount addendum, section 6): this blocker was resolved and R01/R02 are now complete.**
The rest of this section is kept as the historical record of why they were deferred at the time.

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

## 6. Mount addendum: R01/R02, the WinFsp toolchain, and `MountHandle`

Explicitly authorized by the user after the toolchain gap in section 4 was confirmed to be a
system-level, hard-to-reverse change (installing a driver-backed mount toolchain) rather than a
pure code change: "I have already installed some WinFsp with development packages... You have my
approval to install the remaining toolchain and continue to port `mount()`/`mount_s3()`."

### 6.1 The production build profile

`scripts/native/build.py` gained a second `--profile production` alongside the existing
`development` (the previous "not yet implemented" error was removed). Production adds the Go build
tag `cmount` and, only when that tag is set, points `CPATH` at the installed WinFsp SDK's `inc/fuse`
directory (checked at `C:\Program Files\WinFsp\inc\fuse` and the `(x86)` variant, raising a clear
`NativeBuildError` naming both checked paths if neither exists, rather than a confusing Go compiler
error deep in `cgofuse`). No `LDFLAGS` or static linking against `winfsp-x64.lib` is needed -
confirmed by reading upstream rclone's own CI (`native/rclone/.github/workflows/build.yml`):
`cgofuse`'s Windows CGO path resolves `winfsp-x64.dll` dynamically at runtime (`objdump -p` on the
built DLL shows no import-table dependency on it at all), it only needs the header search path at
compile time. `cmd/cmount` (the actual WinFsp/FUSE implementation, via the already-vendored
`github.com/winfsp/cgofuse`) was added to `imports.go` unconditionally: its own
`mount_unsupported.go` build tag already provides an empty stub whenever `-tags cmount` is absent,
so one import line is correct for both profiles. `scripts/native/manifest.py`'s `build_manifest()`
gained a `go_build_tags` parameter driven by the actual tags a given build used (previously it always
read the static, always-empty `NativeTarget.go_build_tags`, so `native-manifest.json` never recorded
`cmount` even when a production build used it - a real, if narrow, pre-existing bug, fixed here).

Verified with two independent real mounts before writing any Python wiring: once via a standalone
`go build -tags cmount` executable (`rclone mount <dir> Z:` - real drive letter, real file read),
and once via the actual `-buildmode=c-shared -tags cmount` library loaded through `ctypes.CDLL` and
driven by `mount/mount`/`mount/listmounts`/`mount/unmount` RC calls exactly as production Python code
uses it.

### 6.2 `vfsOpt`/`mountOpt`: a different parameter convention from every other RC call so far

`mount/mount`'s own RC help text (`cmd/mountlib/rc.go`) shows the critical distinction:
`vfsOpt='{"CacheMode": 2}'`. Unlike every other RC call ported in this migration (which take flat,
underscored `config:`-tag parameter names, e.g. `vfs_cache_mode`, parsed generically by
`rc.AddConfig`/`AddFilter` inside `jobs.NewJob` for *every* call this project's `RcClient.call()`
makes, sync or async - confirmed by reading `librclone/librclone/librclone.go`'s `RPC()`, which
always dispatches through `jobs.NewJob`), `vfsOpt`/`mountOpt` are JSON *objects* decoded with
**standard Go field names** (`"CacheMode"`, `"ReadOnly"`, `"DirCacheTime"`, ...), via
`in.GetStructMissingOK("vfsOpt", &vfsOpt)` - a direct `encoding/json` unmarshal into
`vfscommon.Options`/`mountlib.Options`, not `configstruct`'s tag-based mapping. `fs.Enum[C]`
(`CacheMode`), `fs.Duration` (`DirCacheTime`, `AttrTimeout`, ...), and `fs.SizeSuffix`
(`CacheMaxSize`, `ChunkSize`, ...) all have custom `UnmarshalJSON` implementations confirmed (by
reading `fs/enum.go`, `fs/parseduration.go`, `fs/sizesuffix.go`) to accept **either** a plain string
in the same format as the equivalent CLI flag value (`"full"`, `"1h"`, `"100M"`) **or** a raw number
- so `rc/mount.py`/`mount_ops_embedded.py` pass CLI-flag-shaped strings directly, no numeric enum
conversion needed. `AttrTimeout` in particular lives on `mountOpt` (`mountlib.Options`), not `vfsOpt`
- confirmed by reading `cmd/mountlib/mount.go`, since it is easy to assume every VFS-flavored flag is
a `vfsOpt` field when it is not.

### 6.3 `rc/mount.py`, `MountHandle`: mirroring the serve boundary, not reusing `Mount`

`rc/mount.py` mirrors `rc/serve.py`'s shape exactly (`RcMountClient` Protocol, `MountRef`,
`RcloneRcMountClient`), just with `mount`/`unmount` instead of `start`/`stop`, and `mount`'s extra
`vfs_opt`/`mount_opt`/`config` keyword splits instead of one flat `params` mapping. `MountHandle`
(`mount_handle.py`) mirrors `ServeHandle` - `mount_path`, idempotent `dispose()`, context manager -
and is a **new** class, not a retrofit of `Mount` (`mount.py`): unlike `HttpServer` (H3), `Mount`'s
coupling to a real CLI subprocess runs too deep to retrofit cleanly - `__post_init__`/`close()` call
`wait_for_mount`/`clean_mount`, which poll `.pid`/`.poll()` and shell out to `fusermount`/`mountvol`,
none of which apply to an embedded mount running inside this process's own WinFsp/FUSE goroutine.
`mount/mount`'s own response `mountPoint` (which may differ from the requested one - e.g. Windows
`"*"` auto-assigns a drive letter) is what `MountHandle` remembers and passes back to
`mount/unmount`, never the caller's original input.

### 6.4 `mount()`/`mount_s3()` parameter mapping, including one deliberately preserved quirk

`fetch_mount_embedded()` mirrors `launch_mount()`'s defaults (`allow_writes=False`,
`use_links=True`, `vfs_cache_mode="full"` unless overridden) via `vfsOpt.ReadOnly`/`Links`/
`CacheMode`, plus a flat `transfers` config param (a global `fs.ConfigInfo` field, not a VFS
option). `cache_dir` raises `UnsupportedEmbeddedOperationError` like `other_args`: rclone's
`--cache-dir` sets process-global cache location via `config.SetCacheDir()` at CLI startup, not a
per-mount RC option, so there is no way to honor it per-call over RC at all - not an omission, a
genuine capability gap, documented rather than silently ignored.

`fetch_s3_mount_embedded()` cannot reuse `launch_s3_mount()`'s own logic at all, since that function
builds a `--flag value` string list to append to CLI `other_args`, which embedded execution has none
of. It instead builds the same `vfsOpt`/`mountOpt`/flat-config fields directly, preserving
`launch_s3_mount()`'s exact flag-to-field mapping **bug-for-bug**: the Python parameter
`vfs_disk_space_total_size` sets `--vfs-cache-max-size` (`vfsOpt.CacheMaxSize`), not the
similarly-named `vfsOpt.DiskSpaceTotalSize` field that `vfscommon.Options` separately defines - a
real naming mismatch in the original CLI implementation, deliberately replicated rather than fixed
here, so CLI and embedded execution stay semantically identical for the same parameter value instead
of diverging silently between execution modes (fixing a latent CLI bug is out of scope for a
migration whose whole premise is behavioral parity; it can be raised as its own issue separately).
Similarly, `modtime_strategy` is split across two different underlying option structs depending on
its value despite being exposed as one CLI flag choice: `USE_SERVER_MODTIME` sets a global
`_config.use_server_modtime`, while `NO_MODTIME` sets `vfsOpt.NoModTime` - confirmed by reading
`fs/config.go` (`UseServerModTime`) and `vfs/vfscommon/options.go` (`NoModTime`) directly rather than
assuming both live in the same place.

### 6.5 `close()`, ledger, and test coverage updates

`Rclone` gained `self._mount_handles: set[MountHandle]`, tracked and disposed in `close()` exactly
like `self._serve_handles` (H5) - the same idempotent-dispose reasoning applies unchanged.
`tests/parity/coverage.toml`'s R01/R02 rows are now `cli_parity_tested`; R05's note and parity test
were updated to also cover mount-handle tracking. Unit tests (fake `RcMountClient`) cover
`rc/mount.py`'s wire mapping, `MountHandle`'s dispose idempotency, and
`mount_ops_embedded.py`'s parameter mapping (defaults, `allow_writes`/`use_links`/`vfs_cache_mode`
overrides, the `vfs_disk_space_total_size` → `CacheMaxSize` bug-for-bug mapping, the split
`modtime_strategy` routing, `other_args`/`cache_dir` rejection). Native-DLL tests
(`tests/native/test_mount_ops_embedded_integration.py`, skipped unless the currently built target's
own `native-manifest.json` records the `cmount` build tag) cover a real drive-letter mount/read,
a read-write round trip under `vfs_cache_mode="off"`, `other_args`/`cache_dir` rejection, dispose
idempotency, and `close()` disposing an un-disposed handle, for both `mount()` and `mount_s3()`.
