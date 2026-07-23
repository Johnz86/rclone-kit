# Rclone-kit CLI-to-C-ABI migration ledger

Status: executable migration plan  
Date: 2026-07-23  
Parent architecture: [`rclone_c_abi_implementation_plan.md`](rclone_c_abi_implementation_plan.md)

## Purpose and ownership

This is the living execution document for replacing rclone subprocesses in rclone-kit. The parent
architecture document decides repository ownership, the fork, C ABI, native builds, authorization
workers, and releases. This document tracks the concrete Python migration: every public method,
indirect subprocess dependency, return-type change, RC mapping, bridge extension, compatibility
decision, test, and deletion gate.

Keeping this separate is intentional:

- the architecture should remain stable while individual methods change status;
- this ledger will be edited in nearly every migration pull request;
- it is large enough to obscure the core architectural decisions; and
- final CLI removal requires an auditable checklist rather than a narrative phase description.

The ledger is normative for Phase 6 and Phase 7 of the parent plan. A method is not considered
migrated merely because an RC endpoint with a similar name exists.

## Scope discovered in the current repository

The migration includes more than `CliRcloneBackend`:

- 48 public/static methods on `Rclone`, including helpers that call other public methods;
- command-backed functions under `operations/`;
- `RemoteFS`, which currently starts `rclone serve http` implicitly;
- `File.read_text`, which calls the private CLI `_run` method directly;
- `FilesStream` and the diff parser, which consume process stdout incrementally;
- `Mount` and `HttpServer`, which own `Process` instances;
- configuration discovery through `rclone config paths`;
- executable download, resolution, staging, wheel verification, and installation;
- process-tree and temporary-config cleanup; and
- tests and public result types shaped around `subprocess.CompletedProcess` and `Process`.

Python-native S3/database code is not a CLI boundary. It remains functional during this migration.
Whether to remove provider-specific Python implementations in favor of rclone is a separate product
decision.

## Migration invariants

Every implementation pull request must preserve these rules:

1. **No silent subprocess fallback.** An embedded runtime never launches `rclone.exe` because an RC
   capability or option is missing. It raises a typed unsupported-operation/option error.
2. **No command-line emulation layer.** Do not parse arbitrary CLI argument vectors and reconstruct
   rclone globals inside Python.
3. **One runtime, one immutable config path.** Config files are selected before DLL initialization
   and never switched per operation.
4. **Typed options only.** Known rclone options map to RC parameters, `_config`, `_filter`, mount
   options, or VFS options. Arbitrary `other_args` are transitional CLI-only behavior.
5. **Long operations are RC jobs.** Transfers and checks use `_async: true`, `job/status`,
   `job/stop`, and `core/stats` rather than one blocking FFI call.
6. **Long-lived resources have domain handles.** Mounts and servers use `MountHandle` and
   `ServeHandle`; they do not imitate process IDs or signals.
7. **No Go-to-Python callbacks for streaming.** Python pulls bounded chunks from a bridge-owned
   stream handle.
8. **Domain parity, not log parity.** Tests compare results, filesystem effects, errors, progress,
   and cancellation—not incidental CLI text.
9. **The CLI remains explicit during transition.** Callers select `cli` or `embedded` at client
   construction; a single client does not switch execution model operation by operation.
10. **Deletion follows evidence.** CLI infrastructure remains until every dependent ledger row is
    complete on Windows and Linux.

## Target client and backend model

### Construction

The transitional constructor should become conceptually:

```python
Rclone(
    rclone_conf: Path | Config | None,
    *,
    execution: Literal["embedded", "cli"] = "cli",  # transition only
    library_path: Path | None = None,
    rclone_exe: Path | None = None,                   # deprecated
    runtime: RcloneRuntime | None = None,
)
```

Rules:

- `execution="embedded"` is opt-in until the embedded coverage gate is complete.
- Supplying both `runtime` and `library_path` is invalid; an injected runtime already owns a library.
- `rclone_exe` is accepted only with `execution="cli"` and emits a deprecation warning after the
  embedded default is announced.
- A `Config` value is written to an owner-only temporary file that lives for the runtime lifetime,
  not one temporary file per call.
- A `Path` is resolved absolutely before initialization.
- `None` tells the Go initializer to use rclone's default config path; missing config is represented
  explicitly rather than discovered by launching the executable.
- `Rclone` is a context manager and has idempotent `close()`. It closes resources it owns, but not an
  injected runtime.

Do not add `execution="auto"` while coverage is partial. “Auto” invites environment-dependent
behavior and silent fallback. It may be added after the executable has left normal production use,
where it can mean only “resolve the packaged embedded library.”

### Backend protocols

Replace the command-shaped protocol:

```text
RcloneBackend.run(command)
RcloneBackend.launch(command)
```

with operation-oriented dependencies:

```text
RcClient.call(method, params)
RcClient.start_job(method, params) -> JobHandle
StreamClient.open/read/cancel/close
MountService.mount(...) -> MountHandle
ServeService.start(...) -> ServeHandle
```

Operation modules may accept a narrower protocol containing only what they use. The CLI adapter can
implement legacy operation-level interfaces during transition, but new code must not accept arbitrary
argument arrays.

### RC path normalization

Create one `RcPath` value type before migrating operations. It converts rclone path syntax into the
`fs`/`remote` pairs used by RC methods and handles:

- `remote:path/to/object`;
- `remote:` roots;
- local absolute Windows paths containing a drive colon;
- local POSIX paths;
- trailing slash/root semantics; and
- file-versus-directory expectations.

Do not reproduce path splitting separately in listing, transfer, delete, and config code. Tests must
cover Windows drive letters, remote object names containing literal backslashes, Unicode, empty
remote paths, and leading/trailing slashes.

## Options and compatibility policy

### Typed option objects

Introduce these internal/public value types as needed:

- `GlobalOptions`: transfers, checkers, retries, low-level retries, timeout, metadata,
  multi-thread streams, max backlog, size-only, fast-list, and similar `fs.ConfigInfo` values;
- `FilterOptions`: min/max size, include/exclude rules, and files-from inputs;
- `ListOptions`: recursion, max depth, files/dirs-only, hashes, metadata, and mime/modtime fields;
- `TransferOptions`: operation-specific behavior plus `GlobalOptions` and `FilterOptions`;
- `MountOptions` and `VfsOptions`; and
- `ServeOptions`.

One tested encoder maps these values to the exact JSON names expected in `_config`, `_filter`, RC
method parameters, `mountOpt`, and `vfsOpt`. Validate the encoder against `options/local`,
`options/info`, and real operations from the pinned rclone commit.

### `other_args`

`other_args` is not portable to RC and cannot remain an unlimited escape hatch in the embedded API.
The migration policy is:

1. Existing methods continue accepting it on the CLI backend.
2. Passing a nonempty value to the embedded backend raises `UnsupportedEmbeddedOptionError`, naming
   the method but redacting values that may be secret.
3. Inventory actual options used by repository tests and known callers.
4. Add typed fields for supported options.
5. Deprecate `other_args` when the corresponding embedded method becomes default.
6. Remove it in the C-ABI-only major release.

Do not gradually build an ad hoc CLI parser. A requested option must be modeled, validated, encoded,
and tested explicitly.

### `check`, logging, verbosity, and progress

- `check=True` becomes Python raise-on-error behavior; it is not sent to rclone.
- `verbose` configures rclone/bridge logging and Python progress reporting without exposing secrets.
- `--progress` is replaced by `core/stats` polling and progress events.
- log-file support becomes a runtime log sink or per-resource option where rclone supports it.
- subprocess return codes become typed RC/ABI exceptions and operation status.

## Result and resource types

### `OperationResult`

Introduce a domain result for completed non-streaming operations:

```text
OperationResult
├── ok: bool
├── operation: str
├── job_ids: tuple[int, ...]
├── stats: TransferStats | None
├── warnings: tuple[OperationWarning, ...]
└── attempts: tuple[OperationAttempt, ...]
```

`OperationAttempt` records a partition or retry outcome without a fake command, PID, stdout, or
return code. Errors are raised as typed exceptions when the method's `check` policy requires it.

`CompletedProcess` is deprecated. During one compatibility release it may wrap an
`OperationResult`, preserving `ok` and a synthetic `returncode` (`0`/`1`). Its `stdout`, `stderr`,
`completed`, command formatting, and direct subprocess access cannot be faithfully preserved and
must be documented as CLI-only/deprecated. The embedded-first major release returns
`OperationResult` directly.

### `JobHandle`

`JobHandle` owns an rclone job ID and provides:

- `status()`;
- `stats()`;
- `wait(timeout=None)`;
- `cancel()`;
- context-manager cleanup; and
- a terminal typed result or exception.

It never exposes PID, signals, stdout pipes, or subprocess return codes.

### `MountHandle` and `ServeHandle`

`MountHandle` stores the runtime, actual mount point, source, read-only/cache metadata, and closed
state. `close()` calls `mount/unmount`; runtime shutdown calls `mount/unmountall` for owned mounts.

`ServeHandle` stores the runtime, rclone server ID, actual address, serve type, URL information, and
closed state. `close()` calls `serve/stop`; runtime shutdown calls `serve/stopall` for owned servers.

Compatibility wrappers may retain the existing class names `Mount` and `HttpServer`, but their
`process` attributes and process-control behavior are deprecated and removed. `Process` is not used
inside the embedded implementations.

## Public `Rclone` API migration ledger

Status values used below:

- **Direct RC:** supported by an existing registered RC method;
- **Composite RC:** Python coordinates multiple RC calls;
- **Bridge:** requires a focused rclone-kit Go bridge extension;
- **Python:** no rclone execution boundary;
- **Deprecate:** no meaningful embedded equivalent should be added; and
- **Transitive:** migrates automatically after its dependencies.

All rows are initially **planned** until code and parity tests land.

### Construction, runtime, and control methods

| ID | Public surface | Current behavior | Embedded decision | Compatibility and acceptance |
| --- | --- | --- | --- | --- |
| C01 | `Rclone.__init__` | Resolves `rclone.exe`, discovers config by CLI, creates `CliRcloneBackend` | Create/receive `RcloneRuntime`; initialize DLL once with immutable config | Add explicit execution mode during transition; embedded constructor must create no subprocess. |
| C02 | `upgrade_rclone()` | Downloads/installs executable | **Deprecate**; replace with packaged-native validation and `native_build_info()` | Remains CLI-only during transition; remove console/download path with executable artifact. |
| C03 | `find_rclone_conf()` | Environment, then `rclone config paths` subprocess | **Bridge/Python:** add pre-init default-path query or use initializer-selected default; no executable | Preserve explicit path and `RCLONE_CONFIG` precedence; test Windows/Linux defaults. |
| C04 | `_run()` | Private generic command execution | Remove | All internal callers must be in this ledger; embedded client exposes no generic command runner. |
| C05 | `_launch_process()` | Private generic long-running process | Remove | Replaced by jobs, mount handles, serve handles, or explicit deprecation. |
| C06 | `webgui()` | Starts `rcd --rc-web-gui` | **Deprecate** | Web GUI management is not a storage-library responsibility; do not embed/download GUI assets. |
| C07 | `launch_server()` | Starts external `rclone rcd` | **Deprecate** | Embedded runtime already provides direct RC. External rcd belongs in a separate deployment/tool. |
| C08 | `remote_control()` | Runs `rclone rc` against external address | **Deprecate/replace:** optional separate HTTP `RemoteRcClient`, not a `Rclone` method | Do not change meaning silently. New client is explicit and does not use the C ABI runtime. |
| C09 | `get_verbose()` | Reads Python/global verbosity | **Python** | Keep or replace with runtime configuration property; no migration blocker. |

### Config and metadata methods

| ID | Public surface | Current CLI/dependency | Embedded replacement | Options/result/test decision |
| --- | --- | --- | --- | --- |
| M01 | `obscure()` | `rclone obscure` | **Direct RC:** `core/obscure` | Return exact obscured string; parity fixtures include Unicode and empty input. |
| M02 | `listremotes()` | `rclone listremotes` | **Direct RC:** `config/listremotes` | Rebuild `Remote` values from `remotes`; preserve ordering deliberately. |
| M03 | `config_paths()` | `rclone config paths` | **Direct RC:** `config/paths` after init | Map `config`, `cache`, `temp`; legacy ignored arguments are deprecated. |
| M04 | `config_show()` | `rclone config show [remote] [--obscure/--no-obscure]` | **Bridge:** `rclonekit/config/show` for exact text/sensitivity behavior; add structured `config_get/dump` over `config/get`/`config/dump` | Do not reconstruct secret behavior from assumptions. Test encrypted config and both flags. |
| M05 | `is_s3()` | Parses in-memory `Config` | **Python** initially; optionally `operations/fsinfo` later | No CLI blocker; ensure config changes made through runtime refresh the Python view. |
| M06 | `get_s3_credentials()` | Parses in-memory `Config` | **Python** | Keep secret wrapper/redaction. Decide separately whether exposing credentials remains desirable. |

### Listing, stat, walking, and comparison

| ID | Public surface | Current CLI/dependency | Embedded replacement | Options/result/test decision |
| --- | --- | --- | --- | --- |
| L01 | `ls()` | `lsjson` | **Direct RC:** `operations/list` | `opt.recurse/filesOnly/dirsOnly`; `_config.MaxDepth/UseListR`; Python glob/order post-processing. |
| L02 | `ls_stream()` | Long-running `lsjson` stdout parser | **Bridge:** pull-based list stream, or paged bridge cursor | Must stay bounded for millions of entries; do not implement with one `operations/list` allocation. |
| L03 | `save_to_db()` | Consumes `ls_stream()` pages | **Transitive** from L02 | Database writes remain Python; cancellation closes stream handle. |
| L04 | `print()` | Calls `read_text()` | **Transitive** from B03/B04 | Printing remains Python; no rclone log mixing. |
| L05 | `stat()` | Currently calls `ls()` and selects first file | **Direct RC:** `operations/stat` | Fix semantics to return the exact item or `FileNotFoundError`; parity may reveal existing bug differences. |
| L06 | `modtime()` | Calls `stat()` | **Transitive** from L05 | Preserve textual format. |
| L07 | `modtime_dt()` | Calls `stat()` | **Transitive** from L05 | Preserve timezone-aware conversion. |
| L08 | `size_file()` | Filtered `lsjson` | **Direct RC:** `operations/stat` with files-only | Preserve not-found and multiple-match behavior; exact path should normally remove ambiguity. |
| L09 | `size_files()` | Recursive `lsjson --files-from` | **Direct RC/composite:** `operations/list` plus `_filter.FilesFrom` and `_config.MaxDepth/UseListR` | Temporary files are acceptable first; later bridge can accept list values directly. |
| L10 | `exists()` | Calls `ls()` and catches process error | **Direct RC:** `operations/stat` | Item non-null means true; classify not-found separately from authentication/network failure. |
| L11 | `is_synced()` | `rclone check` return code | **Direct RC:** `operations/check` | Request only needed reports; return `success`; unexpected RC failure is not “not synced.” |
| L12 | `diff()` | Streams `rclone check` report from stdout | **Direct RC first:** `operations/check` arrays; **Bridge stream** if scale tests fail | Preserve `DiffOption`, filters, size-only, one-way. Document materialization threshold. |
| L13 | `walk()` | Repeated `ls()` in Python | **Transitive** from L01 | Preserve breadth/depth-first ordering and lazy generator semantics. |
| L14 | `scan_missing_folders()` | Repeated `ls()`/walk in Python | **Transitive** from L01/L13 | Cross-tree tests compare exact yielded relative directories. |

### Transfers, deletion, and byte access

| ID | Public surface | Current CLI/dependency | Embedded replacement | Options/result/test decision |
| --- | --- | --- | --- | --- |
| T01 | `cleanup()` | `rclone cleanup` | **Direct RC:** `operations/cleanup` | Async job for slow backends; replace `CompletedProcess` with `OperationResult`. |
| T02 | `copy_to()` | `rclone copyto` | **Direct RC:** `operations/copyfile` | Use `RcPath` for `srcFs/srcRemote/dstFs/dstRemote`; `_config` for typed flags. |
| T03 | `copy()` | `rclone copy` | **Direct RC:** `sync/copy` | Always async; map transfers/checkers/retries/multi-thread options through `_config`. |
| T04 | `copy_dir()` | `rclone copy` | **Direct RC:** `sync/copy` | Consolidate with T03; deprecate untyped `args`. |
| T05 | `copy_remote()` | `rclone copy` | **Direct RC:** `sync/copy` | Consolidate with T03; remote roots are full `srcFs/dstFs`. |
| T06 | `copy_files()` | Partitioned `rclone copy --files-from` subprocesses | **Composite RC:** partitioned async `sync/copy` jobs with `_filter.FilesFrom` | Preserve partition parallelism with job handles; return one `OperationResult` containing attempts, not a list of processes. |
| T07 | `purge()` | `rclone purge` | **Direct RC:** `operations/purge` | Correctly split fs/remote; typed error on missing/unsupported purge. |
| T08 | `delete_files()` | Partitioned `rclone delete --files-from [--rmdirs]` | **Composite RC:** `operations/delete` with `_filter.FilesFrom`, then `operations/rmdirs` where requested | Preserve grouping by remote and partial-failure reporting. |
| T09 | `write_bytes()` | Temp local file then `copyto`; S3 may use boto3 | **Direct RC/composite:** temp file plus `operations/copyfile`; keep S3 optimization separately | No raw-body ABI required initially. Protect temp file and delete it on every failure. |
| T10 | `write_text()` | Encodes then `write_bytes()` | **Transitive** from T09 | Preserve UTF-8. |
| T11 | `read_bytes()` | `copyto` remote to temp local file | **Direct RC/composite:** `operations/copyfile` to protected temp file | Avoid loading through serve HTTP. Verify no output and partial-output cleanup. |
| T12 | `read_text()` | Decodes `read_bytes()` | **Transitive** from T11 | Preserve UTF-8 decode behavior. |
| T13 | `copy_bytes()` | `rclone cat --offset --count` to local file | **Bridge stream:** open remote object with offset/count and pull bounded bytes into caller file | Test exact ranges, EOF, zero length, cancellation, and large ranges. |
| T14 | `copy_file_s3()` | Python boto3 | **Python** unchanged | Not a CLI dependency; keep optional dependency and tests. |
| T15 | `copy_file_s3_resumable()` | Python orchestration plus rclone-kit operations/HTTP serve | **Composite/Transitive** | Replace underlying list/read/write/copy/server dependencies; decide later whether direct S3 path remains. |

### Filesystem facade

| ID | Public surface | Current CLI/dependency | Embedded replacement | Options/result/test decision |
| --- | --- | --- | --- | --- |
| F01 | `filesystem()` | Constructs `RemoteFS`, which immediately starts `serve http` | **Composite RC facade** over stat/list/read/write/delete; no implicit server | Constructing a filesystem must bind no port and start no server. |
| F02 | `cwd()` | Calls `filesystem()` | **Transitive** from F01 | Context ownership must keep runtime alive while path is used. |
| F03 | `RemoteFS.exists/is_file/is_dir/ls` | HTTP HEAD and HTML autoindex parsing | **Direct RC:** `operations/stat/list` | Remove dependence on rclone HTML shape and httpx for ordinary filesystem access. |
| F04 | `RemoteFS.read_bytes/write_binary/copy/remove` | Public transfer methods, HTTP server, boto3 | **Transitive/composite** from T02/T08/T09/T11/T13 | Preserve `FileNotFoundError` mapping without swallowing auth/network failures. |
| F05 | `RemoteFS.dispose()` | Stops implicit HTTP subprocess | Runtime/resource ownership only | Be idempotent; no server exists in the normal facade. |
| F06 | `File.read_text()` | Calls private `_run(["cat", ...])` directly | Delegate to associated `Rclone.read_text()` | This direct CLI bypass must be removed before C04. |

### Mount and serve

| ID | Public surface | Current CLI/dependency | Embedded replacement | Options/result/test decision |
| --- | --- | --- | --- | --- |
| R01 | `mount()` | Launches `rclone mount`, returns `Mount(Process)` | **Direct RC:** `mount/mount`; return `MountHandle`/compatible `Mount` | Map CLI flags into `mountOpt`, `vfsOpt`, and `_config`; close via `mount/unmount`. |
| R02 | `mount_s3()` | Builds tuned CLI flags then calls `mount()` | **Composite RC preset** over R01 | Encode every preset field explicitly; reject unknown `other_args`. |
| R03 | `serve_webdav()` | Launches `rclone serve webdav`, returns `Process` | **Direct RC:** `serve/start type=webdav`; return `ServeHandle` | Register WebDAV serve package; credentials never appear in logs. Existing NFS wording is corrected. |
| R04 | `serve_http()` | Launches `rclone serve http`, returns `HttpServer(Process)` | **Direct RC:** `serve/start type=http`; return `HttpServer` facade backed by `ServeHandle` | Keep URL/list/download helpers if useful, but shutdown calls `serve/stop`. |
| R05 | explicit server listing/cleanup | Process enumeration | **Direct RC:** `serve/list/stop/stopall`, `mount/listmounts/unmountall` | Runtime tracks only resources it owns and reconciles against rclone status. |

## Internal and distribution removal ledger

No item in this table is deleted merely because no new code imports it. Its removal gate includes
tests, documentation, public imports, packaging, and downstream compatibility.

| ID | Current component | Replacement | Removal gate |
| --- | --- | --- | --- |
| D01 | `backend.RcloneBackend.run/launch` | Operation/RC protocols | No operation module accepts command tuples. |
| D02 | `CliRcloneBackend` | `EmbeddedRcloneBackend`/`RcClient` | Embedded default has completed all public ledger rows for one compatibility release. |
| D03 | `process.Process` and `ProcessArgs` | `JobHandle`, `MountHandle`, `ServeHandle`, worker supervisor | No public method returns `Process`; worker process management does not use this rclone-specific wrapper. |
| D04 | `process_tree.py` | Worker supervisor termination only | No rclone executable process trees remain; confirm whether generic worker cleanup needs a smaller replacement. |
| D05 | `util.rclone_execute` and live subprocess tracking | `RcClient` and runtime resource tracking | All direct/indirect calls removed and import-boundary test passes. |
| D06 | `util.get_rclone_exe`, executable resolver/cache/downloader | Native library resolver | CLI compatibility mode removed from the supported release. |
| D07 | `util.upgrade_rclone` and `Rclone.upgrade_rclone` | Packaged native validation/update by package release | Install command and docs removed. |
| D08 | `config_discovery._config_paths_via_executable` | Pre-init bridge/default-path logic | C03 parity passes on Windows/Linux. |
| D09 | `CompletedProcess` backed by `subprocess.CompletedProcess` | `OperationResult` | Public deprecation period complete and S3/filesystem callers migrated. |
| D10 | `FilesStream` process stdout parser | Pull-based stream cursor | L02/L03 large-list stress and cancellation pass. |
| D11 | `diff_stream_from_running_process` | `operations/check` result or bridge stream | L12 parity and scale gate pass. |
| D12 | `Mount.process`, process-based close/poll | RC-backed mount handle | R01/R02 mount lifecycle tests pass on supported OS runners. |
| D13 | `HttpServer.process` | RC-backed serve handle | R04 and multipart/download consumers pass. |
| D14 | implicit HTTP server in `RemoteFS` | direct RC filesystem facade | F01–F05 pass; constructing `RemoteFS` opens no listener. |
| D15 | `_run` usage in `File.read_text` | public byte/text API | F06 passes and source search finds no private generic execution. |
| D16 | `scripts/prepare_rclone_artifact.py` upstream executable download | native source build/staging | Transitional dual-artifact release ends. |
| D17 | executable entries in `runtime/platform.py` | native library target/manifest model | Wheels and resolver require only DLL/SO. |
| D18 | executable wheel assets and hashes | library, manifest, license | C-ABI-only release verification rejects executable assets. |
| D19 | `rclone-kit-install-bins` console entry | none or native diagnostic command | Release notes/deprecation complete. |
| D20 | `psutil` process-tree dependency | none, if no other use remains | Dependency search and full tests show it is unused. |
| D21 | executable-focused tests/docs | ABI/runtime/parity tests/docs | Coverage replaced, not simply deleted. |

## Migration waves and file-level work

### Wave A — runtime foundation

Ledger: C01, C03, M01, D08.

Status: C01 (opt-in `execution="embedded"` construction), M01
(`obscure()` over `core/obscure`), and `rc/paths.py`'s `RcPath` are
implemented, unit tested against a fake binding, and native/DLL tested,
including a CLI-reveal round-trip parity check for M01. C03 has an embedded
counterpart (`config_discovery.find_conf_file_embedded`) that queries
`config/paths` through a throwaway runtime instead of shelling out, proven
equivalent to the CLI path in a subprocess-isolated native test (the ABI's
initialize-once-per-process rule means it cannot share a runtime with other
native tests). `rc/options.py` and the machine-readable
`tests/parity/coverage.toml` from "Immediate next migration artifacts" are
not yet started.

Add:

```text
src/rclone_kit/native/abi.py
src/rclone_kit/native/library.py
src/rclone_kit/native/runtime.py
src/rclone_kit/rc/client.py
src/rclone_kit/rc/errors.py
src/rclone_kit/rc/paths.py
src/rclone_kit/rc/options.py
tests/native/
```

Modify `client.py` only enough to support explicit embedded construction and `obscure`. Keep the CLI
default. Validate config selection, allocation ownership, error conversion, and no-subprocess startup.

### Wave B — structured config and basic listing

Ledger: M02–M06, L01, L05–L10, F06.

Refactor `operations/config_ops.py` and the non-streaming portion of `listing_ops.py` to depend on
narrow RC protocols. Add exact `operations/stat/list` adapters and structured config APIs. Remove the
private CLI call from `File.read_text`.

Status: M02 (`config/listremotes`), M03 (`config/paths`), L05 (`operations/stat`), L08
(`operations/stat` with `opt.filesOnly`), and L10 (`operations/stat`) are implemented in
`operations/listing_ops_embedded.py` and `operations/config_ops.py`'s `fetch_config_paths_embedded`,
dispatched from `Rclone` exactly like M01. L06/L07 needed no code at all - they already call
`access.stat(src)`, so they became correct the moment L05 did. Native parity testing against the
real DLL caught a real bug in the first `RcPath`-based draft: `operations/stat`'s `fs` must always be
a navigable root, never a bare file path (rclone's local backend rejects it with "is a file not a
directory"), so `RcPath.as_parent_and_name()` now splits local paths the same way it splits remote
ones, not only remote ones as first written. F06 is also done: `File.read_text()` now delegates to
its associated client's `read_text()` instead of calling the private `_run(["cat", ...])` directly -
this required widening `RPath`/`Remote`'s stored client type from `DomainAccess` to `ListingAccess`
(a strict superset) so the delegation type-checks, with no behavior change for existing CLI callers.
L01 (`ls()`/`operations/list`) is also done: `src=None` still lists remotes via M02's
`config/listremotes` with no separate RC call, `max_depth<0`/`>0`/`None` map to
`opt.recurse`/`_config.MaxDepth` exactly like `--recursive`/`--max-depth`, and `listing_option` maps
to `opt.filesOnly`/`dirsOnly`; glob/order filtering stays pure Python, unchanged from the CLI path.
Native parity tests cover non-recursive, unlimited-recursion, bounded-recursion, and files-only
listings against the real DLL and the same-commit CLI executable on a real nested directory tree.
M05/M06 (`is_s3`/`get_s3_credentials`) needed no adapter or code change at all and are verified
complete: both operate only on `self.config`, never on `self._backend`/`self._rc_client`, so they
already behave identically under either execution mode - a dedicated test asserts zero RC calls
happen. Wave B's only remaining row is M04 (`config_show`, needs a bridge extension for exact legacy
sensitivity behavior); `rc/options.py` has not been needed yet, since every option mapping done so
far has been simple enough to inline directly in its adapter.

### Wave C — checks, walking, and comparison

Ledger: L11–L14.

Implement `operations/check`, normalize its arrays into `DiffItem`, and run parity against current
CLI reports. Keep the current generator API even if the first RC implementation materializes the
result; record memory thresholds before deciding whether L12 needs a stream extension.

Status: L13 (`walk()`) and L14 (`scan_missing_folders()`) needed no code changes and are verified
complete - both call only `Dir.ls()` internally (never `self._backend` directly), so they started
working the moment L01 did. Native parity tests cover breadth-first and depth-first `walk()` over a
real nested tree, and `scan_missing_folders()` over a real src/dst pair with a present-on-both
directory, a fully-missing subtree, and a shared-name-different-contents case.

L11 (`is_synced()`) is also done, via `operations/check` requesting only the report flags it
actually needs (every per-file report array set to `false`) and returning its `success` field
directly. Deliberately does not replicate the CLI backend's "any nonzero return code means not
synced" behavior: an unexpected `RcCallError` (bad path, missing backend) propagates instead of
silently becoming `False`, matching the ledger's own note that this conflation is a bug to leave
behind, not preserve.

L12 (`diff()`) is done too, reusing the same `operations/check` RC method and requesting only the
one report array `diff_option` needs. Its `combined` array uses the exact same `"<prefix> <path>"`
format the CLI backend's own `_classify_diff` already parses (`=`/`-`/`+`/`*`/`!`), so this closes the
row without inventing a new wire format. Unlike the CLI backend - whose `_classify_diff` only
actually handles `COMBINED`, `MISSING_ON_SRC`, and `MISSING_ON_DST`, raising on the other three
`DiffOption` values - every `DiffOption` is supported here, since `operations/check` already returns
`differ`/`match`/`error` arrays directly at no extra cost; native tests confirm this is a strict
improvement, not a parity mismatch, by exercising `DIFFER`/`MATCH` only against the embedded path.
`min_size`/`max_size` map to `_filter.MinSize`/`MaxSize`, `checkers`/`size_only`/`fast_list` map to
`_config.Checkers`/`SizeOnly`/`UseListR`, and `other_args` raises
`UnsupportedEmbeddedOperationError` rather than being silently dropped. This closes out Wave C
completely (L11-L14, with L13/L14 transitive).

### Wave D — simple transfer vertical slice

Ledger: T01–T05, T07, T11–T12.

Add `JobHandle`, transfer option encoding, stats polling, cancellation, and `OperationResult`. Migrate
one-file copy, directory copy, purge, and remote-to-temp reads. Run local and memory backend parity on
both platforms.

Status: T01 (`cleanup`), T02 (`copy_to`), and T07 (`purge`) are done via `operations/cleanup`,
`operations/copyfile`, and `operations/purge` respectively - all synchronous single-call operations
that need no job handle. T11/T12 (`read_bytes`/`read_text`) needed no code at all: both only ever
call `self.copy_to`, so they became correct transitively the moment T02 did, verified with a native
test that reads real file content back through the embedded path. `copy_to`'s `check` parameter keeps
its CLI meaning exactly: `True` (the default) re-raises the RC failure, `False` wraps it into a
failed `CompletedProcess` instead. `CompletedProcess` here wraps one *synthetic*
`subprocess.CompletedProcess` (no real subprocess exists) purely for public-API compatibility during
the transition, per this section's own `OperationResult` compatibility note - it is not meant to be
byte-compatible with a real CLI invocation, and does not carry real stdout/stderr.
`OperationResult`/`JobHandle` were not introduced in that first slice: none of T01/T02/T07/T11/T12
needed an async job (they are all synchronous RC calls), so that design work was deferred to T03–T05
(`copy`/`copy_dir`/`copy_remote`, all "Always async" per the ledger). That deferred work is now done,
following the normative design in
[`native_c_abi_wave_d_review_and_design.md`](native_c_abi_wave_d_review_and_design.md) (which
supersedes ad hoc `sync/copy` + `_config.Retries` as "enough" for `copy()`, since the pinned rclone's
RC `sync/copy` handler does not run the CLI's high-level retry loop):

- `rclone_kit/operation.py` (`JobState`/`JobStatus`/`TransferStats`/`OperationAttempt`/
  `OperationResult`/`OperationWarning`), `rclone_kit/rc/jobs.py` (`RcJobRef`/`RcloneRcJobClient`,
  strict wire parsing against the real pinned build), and `rclone_kit/job.py` (`JobHandle`/
  `_JobMonitor`, one lazily-started background poller per embedded client) form the job
  infrastructure: `OperationResult`s reach callers with real `stats`, and terminal state survives
  rclone's own `job/status` expiry window.
- A small downstream Go RC endpoint, `rclonekit/copy` (`native/rclone`'s
  `librclone/rclonekit/rc/copy.go`), reproduces `cmd.Run`'s high-level retry loop (attempt count from
  `_config.Retries`, `fs.CountError` classification, reset-between-attempts, context-aware retry
  sleep) scoped to the calling job's own accounting group, so concurrent library jobs never share
  error state. This is committed locally in the `native/rclone` submodule
  (`rclone-kit/integration-v1`, commit `d498adafa`) but **not yet pushed** to its GitHub remote, so
  the parent submodule pin has not moved - pushing that commit and moving the pin is a separate,
  explicitly-authorized step, not part of this ledger update.
- `Rclone.start_copy()` is the new canonical embedded-only entry point (raises
  `EmbeddedOnlyOperationError` under `execution="cli"`, since `JobHandle`'s async job model has no
  blocking-process equivalent): it encodes `TransferOptions`/`RcFsSpec`, starts `rclonekit/copy`, and
  returns a `JobHandle`. `copy()`'s embedded path calls `start_copy(...).wait()` with `copy()`'s own
  historical tuned defaults (checkers 1000, transfers 32, low-level retries 10, retries 3) applied at
  its own call site, not baked into `TransferOptions`; `copy_dir()`/`copy_remote()` share the same
  engine with rclone's own defaults and their historical non-raising failure behavior, rejecting
  nonempty `args` rather than silently dropping them. `Rclone.close()` now cancels and waits for
  every job a client started (regardless of runtime ownership) before finalizing an owned runtime,
  raising `OperationShutdownError` and leaving the runtime open if a job cannot be confirmed settled
  in time.

Phase D7 (retrofitting T01/T02/T07/T11/T12 onto the shared job/result architecture) is now done.
`cleanup_embedded`/`copy_file_to_embedded`/`purge_dir_embedded` each start their RC method
(`operations/cleanup`/`operations/copyfile`/`operations/purge`) as an asynchronous job through the
same `_JobMonitor` `start_copy()` uses - via the RC framework's generic `_async: true` mechanism, not
a bespoke Go endpoint, since none of these three need a retry loop of their own - then immediately
wait on the resulting handle. This closes design review finding F5 (a synchronous RC call previously
held the runtime lock for the whole operation) and F6 (they now return a real `OperationResult`, never
a synthetic `subprocess.CompletedProcess`); `Rclone`'s own methods wrap the result via
`CompletedProcess.from_operation_result()`.

`copy_to(check=True)` now raises `OperationFailedError` (the execution-independent hierarchy) on
failure instead of a raw `RcCallError`, and `read_bytes()`/`write_bytes()` catch both that and the
CLI's `subprocess.CalledProcessError`, translating either to `RcloneCommandError` - so the raised type
no longer depends on execution mode, closing design review finding F4 for T02/T11/T12.
`tests/parity/coverage.toml`'s `T01`/`T02`/`T07`/`T11`/`T12` rows now record
`failure_contract_complete = true` (T01/T07 never raise, so the field only meaningfully applies to
T02/T11/T12, but is recorded uniformly).

Closing Phase D7's item 5 (relative local filename coverage) surfaced a real, previously-undetected
bug, not just a documentation gap: `RcPath`'s F8 fix (accepting a bare local relative reference like
`"file.txt"` or `"."`) assumed rclone would re-resolve that relative string against the *current*
working directory on every call. In fact rclone's `Fs` cache keys an instance by the literal string
handed to it, resolving a relative reference against the current directory only the *first* time it
is used against a given runtime; every later call with that same literal string reuses that first
resolution, even after this process's `cwd` has since changed - and the embedded runtime is
long-lived and shared, unlike a one-shot CLI subprocess. This was caught empirically (not by code
inspection) by a new native test combining an absolute source with a relative destination
(`test_copy_to_relative_basename_destination_also_works`), confirmed with two standalone reproduction
probes against the real built library (one for `copy_to`'s bare `"."`, one for `purge`'s bare relative
directory name), and fixed at the root: `RcPath.parse()` now absolutizes any bare local reference
(via `Path(path).resolve()`) at parse time, before it ever crosses the RC boundary, so every current
consumer (`copy_to`, `purge`, `cleanup`, and - transitively, since they share `RcPath`/`encode_fs_spec`
- the Wave B/C listing operations and `start_copy`) gets the fix. `encode_fs_spec` was updated to
return the absolutized `target.fs` for a local target instead of the original, possibly-relative
`spec` string. See `rc/paths.py`'s `_resolve_local` docstring for the full explanation.

### Wave E — filtered and partitioned operations

Ledger: T06, T08, L09.

Implement files-from `_filter` encoding and multi-job aggregation. Preserve partial-success details
without constructing fake subprocesses. Stress test thousands of files, multiple remotes, duplicate
inputs, empty inputs, cancellation, and one partition failing while others complete.

Wave E is now done, following the normative design in
[`native_c_abi_wave_e_review_and_design.md`](native_c_abi_wave_e_review_and_design.md) (which
supersedes this table's original "partitioned async `sync/copy` jobs" plan for T06, for the same
reason Wave D moved `copy()`/`copy_dir()`/`copy_remote()` off a bare `sync/copy`: it does not
reproduce the CLI's high-level retry loop):

- `rclone_kit.rc.jobs.parse_operation_attempts()` parses `rclonekit/copy`'s `output["attempts"]`
  array, wired into `job.py`'s `_settle_terminal` so every `OperationResult` it produces carries real
  attempts - not just for T06's aggregate, but retroactively for Wave D's own `start_copy()`/`copy()`
  too, closing a gap Phase D7 had deliberately deferred. `operations/transfer_options.py`'s
  `TransferOptions` gained `retries_sleep`/`timeout`/`max_backlog`/`metadata` fields (the same
  `fs.ConfigInfo` `_config` keys `copy_files_partitioned` already used on the CLI side), so one
  options type still covers every embedded copy/sync-shaped RC call.
- `copy_files_embedded()`/`delete_files_embedded()` (`operations/transfer_ops_embedded.py`) partition
  exactly like their CLI counterparts (`group_files()`, unchanged) - one `rclonekit/copy` job per
  common-prefix partition for T06, one `operations/delete` job per remote/common-prefix partition for
  T08 (followed by `operations/rmdirs(remote="", leaveRoot=true)` when `rmdirs=True` and that
  partition's delete succeeded, reproducing `delete`'s own `--rmdirs` sequence exactly, not a new
  one). Every partition is started before any is waited on, and every partition is collected before
  `check=True` can raise, so a partial failure never loses a still-running sibling's result.
  `_aggregate_results()` folds every partition into one `OperationResult` (`job_ids`/`attempts`
  spanning every partition, one `OperationWarning` per failed partition), matching this row's own
  ledger note ("one `OperationResult` containing attempts, not a list of processes") without
  changing `copy_files()`'s existing public `list[CompletedProcess]` return type - the embedded path
  returns a single-element list wrapping the aggregate, deferring any public-signature harmonization
  to Wave I. `max_partition_workers` is reinterpreted as a start-batch size rather than a Python
  thread pool: an RC job is already concurrent on rclone's side the moment `start()` returns, so no
  thread pool is needed for an already-async call.
- `fetch_size_files_embedded()` (`operations/listing_ops_embedded.py`) is a single, non-partitioned
  `operations/list` call (`opt.filesOnly`/`opt.recurse` plus `_filter.FilesFrom`, `_config.UseListR`
  when `fast_list=True`) - correcting this row's original `composite_rc` categorization to
  `direct_rc`, since a single call already covers every requested file regardless of how many
  directories/remotes they span (only T06/T08 partition, because only they *write*, and a write per
  remote/directory boundary is the thing that needs isolating). The `<2`-file shortcut still
  delegates to the already-embedded `fetch_size_file_embedded()`. The dedup/warn/prefix-strip
  post-processing that turns a flat file listing into a `SizeResult` was extracted into
  `listing_ops.build_size_result()`, shared by both backends instead of duplicated.
- Empirically confirmed (not assumed) for both T06 and T08: a `FilesFrom` entry naming a file that
  does not exist is not an error - `sync.CopyDir`/`operations.Delete` simply do not visit it during
  their walk, matching each command's own CLI behavior. `_filter.FilesFrom`/`_config` compose with
  every RC method used here (including the downstream `rclonekit/copy` endpoint) because
  `fs/rc/jobs/job.go`'s `NewJob` parses both generically for any call, sync or async, before
  dispatching to the method-specific handler - confirmed by reading that dispatch path, not assumed
  from the RC framework's documented intent.

### Wave F — streaming and bytes

Ledger: L02–L04, T09–T10, T13, D10–D11.

Add the pull-based bridge only after defining bounded-buffer, handle, error, EOF, cancel, and close
semantics. Migrate database listing and byte ranges. Run memory/handle leak tests and interrupted
consumer tests.

Wave F is now done, following the normative design in
[`native_c_abi_wave_f_review_and_design.md`](native_c_abi_wave_f_review_and_design.md). D10/D11 are
not action items here - they are this migration plan's own internal/distribution-removal ledger
(section "Internal and distribution removal ledger"), and both name their gate as "L02/L03 ...
pass"/"L12 ... pass": they record when it becomes safe to *delete* `FilesStream`'s CLI-only
process-stdout parser and `diff_stream_from_running_process`, which belongs to Wave I, not this one.

- **T09 (`write_bytes`)/T10 (`write_text`)/L04 (`print`) turned out to already be functionally
  complete**, discovered by checking before assuming: `write_bytes()` already calls `copy_to()`
  (embedded since Wave D Phase D7), and `print()` only ever calls `read_text()` (embedded since the
  same phase). No new code was needed for any of the three - only a dedicated native parity test
  each and a ledger correction (T09's original `composite_rc` decision is now `transitive`).
- **L02 (`ls_stream`)** needed a genuinely new bridge: `operations/list`'s own RC handler
  (`rcList` in `fs/operations/rc.go`) accumulates the *entire* listing into one Go slice before
  returning it as one JSON blob - read directly from source, not assumed - which cannot bound memory
  for "millions of entries" the way this row requires. `librclone/rclonekit/rc/liststream.go` (local-
  only submodule commit, not pushed) adds three new downstream RC methods -
  `rclonekit/liststream/open`/`.../next`/`.../close` - backed by a small handle table and a bounded
  (1024-item) channel per stream, reusing the existing `RcloneKitRPC` C function with no ABI change.
  `next(streamId, maxItems, timeoutMs)` is bounded-wait, not indefinite-block, since
  `RcloneRuntime.call()` serializes all RC dispatch through one lock and an indefinitely-blocked
  `next()` would starve even an emergency `close()` from another thread. `EmbeddedFilesStream`
  (`rclone_kit/embedded_file_stream.py`) exposes the exact same public surface as the CLI-backed
  `FilesStream`, so `save_to_db()` (L03) needed zero changes - confirmed transitively with its own
  dedicated test rather than left as an inferred claim.
- **T13 (`copy_bytes`)** is bounded by construction (an explicit `offset`/`count`), so - unlike L02 -
  it needed no cursor, just one new downstream RC method, `rclonekit/readrange`, opening the object
  with `fs.RangeOption{Start, End}` (HTTP Range semantics, inclusive end) and streaming directly into
  a local file via `io.CopyN`. A range extending past the object's actual end is not an error
  (`io.CopyN`'s `io.EOF` for a short copy is treated as success, reporting the real byte count) -
  verified empirically for an exact range, zero length, offset exactly at EOF, a range past EOF, a
  full-file range, and a missing source object. Routed through the same `_JobMonitor` async-job
  engine as T01/T02/T07 (not a bare synchronous call) purely so a large in-flight range download can
  be observed/cancelled like any other embedded operation.
- **A pre-existing bug was found and fixed while testing L03**, unrelated to embedded execution
  specifically: `rclone_kit.db.models.create_file_entry_model` built its dynamic `FileEntryConcrete`
  subclass inheriting `FileEntry.size`'s `sa_column=Column(BigInteger)` as-is - a single `Column`
  instance built once, at `FileEntry`'s own class-definition time, and shared by every subclass
  unless overridden. Calling `create_file_entry_model` twice for two different tables within the same
  process (which no prior test happened to do) crashed with `ArgumentError` the second time, since a
  `Column` can only belong to one `Table`. Fixed by re-declaring `size` with a fresh `Column` inside
  the dynamic subclass itself.

### Wave G — direct filesystem facade

Ledger: F01–F05, T15, D14–D15.

Rewrite `RemoteFS` to use RC operations directly. Stop launching HTTP serve at construction. Adapt
multipart/resumable consumers to direct list/stat/read or an explicitly requested `ServeHandle`.

Wave G (F01–F05) is now done, following the normative design in
[`native_c_abi_wave_g_review_and_design.md`](native_c_abi_wave_g_review_and_design.md). D14/D15 are
not action items here, for the same reason as D10/D11 in Wave F (their gate is "F01–F05 pass"/"F06
passes" - they record when it becomes safe to remove `RemoteFS`'s old HTTP-serve-based
implementation and `File.read_text`'s private generic execution, which belongs to Wave I).

- **F04 turned out to already be functionally complete**: `RemoteFS.read_bytes`/`write_binary`/
  `copy`/`remove` already called `rclone.read_bytes()`/`write_bytes()`/`copy_to()`/`delete_files()`
  directly - embedded-capable since Waves D/E/F - and never depended on the HTTP server at all.
  Corrected the ledger's original `composite_rc` decision to `transitive`.
- **F01/F03**: `RemoteFS.__init__` no longer calls `serve_http()` (an explicit, lazy `serve()`
  method exists for a future consumer that genuinely needs a real `HttpServer`); `exists`/`is_dir`/
  `is_file`/`ls` now go straight through `rclone.exists()`/`stat()`/`ls()` (`operations/stat`/
  `operations/list`) instead of HTTP HEAD/autoindex parsing, removing the dependency on rclone's
  HTML shape and `httpx` for ordinary filesystem access. This may incidentally fix a known,
  currently-skipped cloud-test bug (`tests/cloud/test_fs_remote.py`'s
  `test_create_and_remove_remote_fs`, skipped for HTTP-autoindex staleness immediately after a
  delete) - plausible, since the rewrite bypasses that autoindex cache entirely, but not yet
  confirmed against a live bucket from this environment.
- **T15 (`copy_file_s3_resumable`) is genuinely blocked on Wave H, not completed here**: read before
  assuming - `upload_parts_resumable()` (`s3/multipart/upload_parts_resumable.py`) calls
  `self.serve_http(src_dir)` as a load-bearing part of the resumable-upload flow itself. `serve_http`
  is not embedded-capable until Wave H ports serve; this correctly raises
  `UnsupportedEmbeddedOperationError` under `execution="embedded"` today, per the no-silent-fallback
  invariant. `tests/parity/coverage.toml`'s T15 row stays `planned`, with its notes naming this exact
  blocking call site.

### Wave H — serve and mount resources

Ledger: R01–R05, D12–D13.

Implement typed handles and runtime-owned cleanup. Add explicit serve imports in the Go bridge and
the required Windows cmount/Linux FUSE production build profiles. Run privileged platform tests.

Wave H is partially done, following the normative design in
[`native_c_abi_wave_h_review_and_design.md`](native_c_abi_wave_h_review_and_design.md): R03/R04/R05
are complete; R01/R02 (`mount()`/`mount_s3()`) are genuinely blocked on the Windows cmount/Linux FUSE
production build toolchain this section itself already calls out as needed, not attempted.

- **R03/R04 (`serve_webdav`/`serve_http`)**: verified empirically, before writing any Python code,
  that `cmd/serve/http`/`cmd/serve/webdav` need no platform driver (unlike mount) - both import
  `cmd` (the shared root/helper package every leaf command needs), not `cmd/all`, and a real
  `serve/start` → HTTP GET/webdav → `serve/stop` round trip worked against the freshly built DLL
  before any Python wiring existed. `serve_http()`/`serve_webdav()` now dispatch to `serve/start
  type=http`/`type=webdav` via a new `rc/serve.py` boundary module (mirroring `rc/jobs.py`/`rc/
  list_stream.py`'s own conventions) and a new, deliberately minimal `ServeHandle` (`id`/`addr`/
  idempotent `dispose()` - matching what `serve_webdav()`'s historical `Process` return was actually
  used for, confirmed by search: no consumer ever touched `Process`-specific behavior on it).
  `HttpServer` needed no behavioral changes at all to host an embedded server: it only ever used its
  stored `process` for an alive-check and `.dispose()`, never anything `Process`-specific, so only
  its constructor's type was widened (a two-line `_DisposableServerHandle` protocol) to accept a
  `ServeHandle` in place of a `Process`. A pre-existing "via NFS" docstring/error-message bug on
  `serve_webdav()` (this method serves WebDAV, not NFS) was fixed while touching this code, per this
  row's own ledger note.
- **R05 (server listing/cleanup)**: `Rclone` now tracks every `ServeHandle` it starts and disposes
  each (idempotently) in `close()`, mirroring `_JobMonitor.shutdown()`'s established Wave D pattern -
  "the runtime tracks only resources it owns." `cmd/mountlib` (the `mount/*` RC method registry, with
  no dependency on `cmd`, FUSE, or WinFsp) was also registered and verified empirically:
  `mount/listmounts` returns an empty list and `mount/unmountall` succeeds trivially with nothing
  registered to unmount, while `mount/mount` itself correctly fails ("mount option specified is not
  registered, or is invalid") rather than crashing or silently no-opping - but with no actual mount
  support yet, there is nothing for a mount-listing API to reconcile, so no public surface was added
  for it this wave.
- **R01/R02 (`mount`/`mount_s3`)**: not completed, and not silently skipped either - confirmed via
  the same empirical `mount/mount` call above that the RC method is already wired and fails honestly.
  Actually porting these needs a real platform mount implementation (`cmd/mount`/FUSE on Linux,
  `cmd/cmount`/WinFsp on Windows) compiled into a production build profile that does not exist yet
  (`scripts/native/build.py --profile` only implements `development`, no-mount, today) - a distinct,
  substantial toolchain undertaking from this wave's RC-wiring work.

### Wave I — public compatibility transition

Ledger: C02, C06–C08, D01–D11, D19–D21.

Deprecate generic CLI control and subprocess-shaped results. Make embedded execution the default only
after every non-deprecated method is complete. Publish one compatibility release retaining explicit
CLI mode and collect failures before final removal.

Wave I is partially done, following the normative design in
[`native_c_abi_wave_i_review_and_design.md`](native_c_abi_wave_i_review_and_design.md): C02/C06–C08
(deprecation) are complete. This wave's own exit gate - "make embedded execution the default only
after every non-deprecated method is complete" - is **not** reachable yet, since `mount()`/
`mount_s3()` (Wave H, R01/R02) remain genuinely blocked on FUSE/WinFsp production build toolchain
work that does not exist yet. No default was flipped and no CLI-only code was removed; D01–D11/
D19–D21 (this plan's own internal/distribution-removal ledger) stay as they were, since every one of
their gates is tied to other rows reaching `complete` first, which has not happened.

- **C06/C07/C08** (`webgui`/`launch_server`/`remote_control`) now emit `DeprecationWarning` on every
  call, with behavior completely unchanged - each is CLI-only with no planned embedded port (web GUI
  management isn't a storage-library concern; embedded execution already provides direct in-process
  RC with no external `rclone rcd` needed; `remote_control()`'s "drive a separate, externally-
  addressed rclone process" meaning is unrelated to this client's own execution mode and a standalone
  RC HTTP client is planned to replace it, not yet built).
- **C02** (`upgrade_rclone`) also now warns, but additionally gained a real replacement this row's own
  ledger note named: `Rclone.native_build_info()`, wrapping the already-existing (since the bridge's
  first slice) `RcloneRuntime.build_info()` - ABI version, rclone version/commit, Go version, build
  tags, target. Embedded-only (`EmbeddedOnlyOperationError` under `execution="cli"`): the native
  library is linked at build/package time, never downloaded/verified per call the way
  `upgrade_rclone()`'s CLI-mode flow is.

### Wave J — executable-free wheels

Ledger: D02, D06–D07, D16–D18.

Remove executable staging, runtime download, resolver, hashes, and wheel assets. Continue building a
matching executable in CI and publishing it as a GitHub diagnostic/upstream artifact, but do not
install it with the Python package.

Wave J is blocked and was not started - see
[`native_c_abi_wave_j_review_and_design.md`](native_c_abi_wave_j_review_and_design.md). Every one of
D02/D06/D07/D16-D18's own removal gates requires CLI compatibility mode to already be removed from
the supported release, which requires Wave I's exit gate (embedded made the default), which requires
Wave H's R01/R02 (`mount`/`mount_s3`) to be ported, which requires FUSE/WinFsp production build
toolchain work that does not exist yet. Removing the executable resolver/wheel assets now would break
every `execution="cli"` deployment - still this library's default - for no compensating benefit,
since embedded execution cannot yet reach parity. This is a documented, verified blocker, not an
oversight: nothing further can responsibly be designed or built for this wave until that precondition
chain is satisfied.

## Per-row implementation checklist

Each ledger row moves through these statuses in its pull request:

```text
planned
  -> adapter implemented
  -> unit tested
  -> actual DLL/SO tested
  -> CLI parity tested
  -> Windows passed
  -> Linux passed
  -> documentation/deprecation updated
  -> embedded default enabled for row
  -> CLI path removable
  -> complete
```

The pull request must record:

- RC method and exact request JSON;
- option fields and their encoded destination;
- sync/async decision;
- result and exception mapping;
- progress and cancellation behavior;
- resource ownership and close behavior;
- memory/streaming behavior;
- known backend differences;
- Windows/Linux test evidence; and
- whether a public API change requires a deprecation or major version.

## Parity test template

Every operation-level parity test should use the same fixture/config and perform:

1. arrange independent source/destination roots for CLI and embedded runs;
2. call the public operation with equivalent typed options;
3. normalize domain results rather than command output;
4. compare directory trees, object data, metadata where promised, and error categories;
5. compare cancellation and partial-failure behavior for long work;
6. assert embedded execution created no `rclone` child process;
7. assert no secrets appear in captured logs/exceptions; and
8. clean every job, mount, server, stream, temporary file, and runtime.

Minimum fixture layers:

- memory backend for fast semantic tests;
- local filesystem for path, timestamp, range, and permission behavior;
- fake/controlled OAuth provider for authorization interactions;
- live S3-compatible storage for backend-specific behavior; and
- protected Google Drive tests for OAuth and a representative non-S3 backend.

## Default-switch gate

Embedded execution becomes the default only when:

1. C01 and all non-deprecated M/L/T/F/R rows are complete.
2. `other_args` usage has a typed replacement or an explicit documented incompatibility.
3. All public return types have a released compatibility/deprecation path.
4. Large listing, diff, and byte-range memory behavior meets measured limits.
5. Runtime shutdown leaves no jobs, mounts, servers, streams, or temp configs.
6. Windows and Linux packaged-wheel integration tests exercise the actual embedded default.
7. Authorization workers and normal operations can coexist without config/global-state collisions.
8. An import/source test prevents new subprocess calls outside the temporary CLI compatibility
   package.
9. Release notes identify deprecated control-plane methods and their replacements.
10. Explicit CLI mode remains available for one compatibility release, with no silent fallback.

## Executable-removal gate

The executable is removed from wheels only when:

1. the default-switch release has been in use for one full compatibility cycle;
2. every D-row except diagnostic CI executable production is complete;
3. `git`/source searches find no production `rclone_execute`, `CliRcloneBackend`, `ProcessArgs`,
   `get_rclone_exe`, or executable asset resolution;
4. `psutil` and process-tree code are removed if otherwise unused;
5. installed-wheel tests run with no `rclone` executable on `PATH` or in package data;
6. mount and serve work through RC handles on supported systems;
7. streaming/raw-body features use the bridge without HTTP/CLI workarounds unless explicitly part of
   the public serve feature;
8. the build and release pipeline rejects accidental executable inclusion;
9. documentation no longer tells library users to install or locate rclone; and
10. the separately published diagnostic executable is built from the identical fork commit as the
    library.

## Decisions that must not be deferred into coding

The following are now decided:

- The migration ledger is a separate document and is linked from the architectural plan.
- Embedded clients never fall back silently to CLI.
- Arbitrary `other_args` are deprecated rather than parsed indefinitely.
- `webgui`, `launch_server`, and the existing `remote_control` method are deprecated; an optional
  external-RC HTTP client is a separate abstraction.
- `RemoteFS` becomes a direct RC facade and stops starting an HTTP server implicitly.
- Exact legacy `config_show` behavior gets a focused Go bridge call; structured config APIs use
  existing RC endpoints.
- Normal full-file reads/writes initially use protected local temporary files plus
  `operations/copyfile`, avoiding an unnecessary raw-body ABI.
- Byte ranges and large listing streams use a pull-based handle extension.
- `OperationResult` and typed resource handles replace subprocess-shaped return values.
- Both execution backends coexist only during a deliberate compatibility period.
- The executable remains a CI/upstream diagnostic artifact after it leaves the wheel.

## Immediate next migration artifacts

Before implementing more than the first native smoke call, create:

1. `tests/parity/coverage.toml` or an equivalent machine-readable copy of the row IDs and statuses;
2. an import-boundary test that identifies every production subprocess call;
3. `RcPath` and its Windows/POSIX tests;
4. typed RC option encoders validated against the pinned rclone version;
5. `OperationResult`, `JobHandle`, and their compatibility policy; and
6. an embedded-backend test that fails if `rclone.exe` is spawned.

The machine-readable coverage file should name the Markdown row ID, public method, owner module,
parity test, Windows/Linux state, and removal dependencies. CI can then reject an executable-removal
change while incomplete rows remain.
