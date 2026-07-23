# Rclone-kit C ABI implementation plan

Status: architectural decision and delivery plan  
Date: 2026-07-23  
Target toolchain: Go 1.26.5, CPython 3.13+, Windows amd64 and Linux amd64

This document supersedes the subprocess-first recommendation in
[`rclone_c_shared_library_investigation.md`](rclone_c_shared_library_investigation.md) for the
chosen product direction. The project will adopt a Python-to-Go C ABI as its primary rclone
integration. Subprocess execution remains only as a migration aid and a differential test oracle.

## Executive decision

The future architecture will use:

1. an organization-owned fork of `rclone/rclone`;
2. a small rclone-kit-specific C bridge compiled inside that fork;
3. a pinned checkout of the fork at `native/rclone` in rclone-kit;
4. a typed Python `ctypes` layer over the versioned C ABI;
5. rclone's RC methods as the primary operation protocol;
6. Python worker processes for authorization-session isolation; and
7. rclone-kit-owned native builds, wheel packaging, verification, and releases.

The current `reference/rclone` checkout is **not** a suitable production source tree. `reference/`
should contain research notes, upstream snapshots used for inspection, and implementation records.
It must not be an implicit build input. A reference checkout is not pinned by the parent repository,
does not provide an auditable dependency transition, and makes it too easy to build uncommitted or
unpublished Go changes.

The canonical layout will be two Git repositories:

```text
GitHub organization/user
├── rclone                         fork of rclone/rclone
│   ├── minimal upstreamable OAuth change
│   └── downstream rclone-kit C bridge
└── rclone-kit                     Python package and product release
    └── native/rclone              Git submodule pinned to the fork
```

Do not create a third repository just for shared-library binaries. Native artifacts should be built
from the pinned fork by rclone-kit's CI, included in platform wheels, and attached to the same
rclone-kit release for diagnostics. This keeps source pinning, Python compatibility, wheel contents,
and native provenance in one release decision.

## Product objectives

The implementation must preserve these outcomes:

- Python callers use rclone functionality without running the `rclone` executable in production.
- Rclone, not Python, owns provider-specific authorization, token exchange, token persistence, and
  refresh behavior.
- A caller can present an externally reachable authorization link while the rclone callback listener
  remains private inside a FastAPI process, container, or worker.
- Multiple authorization sessions are isolated from rclone's process-global config, OAuth, and job
  state.
- The same pinned rclone source builds a normal executable and the shared library, allowing manual,
  upstream, and differential testing.
- Windows and Linux wheels install without Go, GCC, or rclone on the target machine.
- The final Python API exposes domain operations, jobs, mounts, servers, and authorization sessions;
  it does not expose C pointers or pretend that an RC call is a subprocess.
- Existing rclone-kit features remain on the CLI backend until their C ABI replacements pass parity
  tests. Removal of the bundled executable is a final migration gate, not an early cleanup.

## Decisions and non-decisions

| Question | Decision |
| --- | --- |
| Is `reference/rclone` the build source? | No. It remains research-only. |
| Do we need a fork? | Yes, until the listener/redirect change is released upstream, and for the downstream C bridge. |
| Do we need a separate native-library repository? | No. The bridge lives in the rclone fork; distribution belongs to rclone-kit. |
| How is rclone source pinned? | A `native/rclone` Git submodule plus a checked manifest. |
| Which protocol crosses the ABI? | UTF-8 JSON for RC calls, plus a very small versioned lifecycle/memory API. |
| Do we expose upstream's experimental ABI directly? | No. Wrap its internal implementation behind rclone-kit's own versioned symbols. |
| Is the library loaded into FastAPI's main process for authorization? | No. Authorization uses a spawned Python worker process per active session. |
| Are normal operations always workers? | No. A single-user client may use one in-process runtime; isolated/persistent workers can be added for multi-tenant runtimes. |
| Does the wheel initially contain both executable and library? | Yes, during migration. The final C-ABI-only wheel is a later major-version decision. |
| Are mount and serve ignored? | No. They require explicit RC registration, typed lifetime handles, and platform-specific testing. |
| Is arbitrary CLI compatibility promised by the C ABI? | No. Existing public operations must be mapped deliberately to RC or a focused bridge extension. |

## Repository and branch structure

### Rclone fork

Create or use a public fork such as `Johnz86/rclone`. Configure remotes in its local worktree as:

```text
origin    -> the maintained fork
upstream  -> https://github.com/rclone/rclone.git
```

Maintain three branch roles:

- `master`: tracks upstream `master` without downstream product commits;
- `oauth-public-redirect`: the smallest upstreamable listener/redirect change and its tests; and
- `rclone-kit/integration-v1`: a release-based integration branch containing the tested OAuth commit
  plus the rclone-kit C bridge.

The upstream pull request must be created from `oauth-public-redirect`, not from the integration
branch. The integration branch may contain downstream-only ABI files and RC registrations that
would distract from the OAuth review. When upstream merges the OAuth change, replace the cherry-pick
with the upstream commit and eventually a release tag; do not rewrite already published rclone-kit
artifacts.

Base product builds on an explicit upstream release where possible. Use current upstream `master`
for contributing and validating the pull request, but do not make a moving branch the source of a
published wheel.

### Rclone-kit repository

Add the maintained fork as a submodule:

```text
rclone-kit/
├── .gitmodules
├── native/
│   ├── README.md
│   ├── toolchain.toml
│   └── rclone/                       Git submodule
├── scripts/
│   └── native/
│       ├── build.py
│       ├── verify.py
│       ├── write_manifest.py
│       └── smoke.py
├── src/rclone_kit/
│   ├── native/
│   ├── rc/
│   └── authorization/
├── tests/
│   ├── native/
│   ├── authorization/
│   └── parity/
└── reference/                        documents only
```

The submodule entry itself is the immutable rclone source pin. `native/toolchain.toml` records inputs
that are not represented by the Git commit:

```toml
schema = 1
go_version = "1.26.5"
c_abi_version = 1
rclone_upstream_version = "1.74.x"
rclone_integration_branch = "rclone-kit/integration-v1"
windows_compiler = "mingw-w64-gcc"
linux_wheel_policy = "manylinux2014_x86_64"
```

The exact upstream base and fork commit are also copied into the generated native manifest, but the
submodule pointer remains authoritative. CI must fail if the checked-out submodule commit and the
generated manifest disagree.

### Local two-repository workflow

A change that affects both projects uses two reviewable commits:

1. Work and test in `native/rclone` on a named fork branch.
2. Commit and push the Go change to the rclone fork.
3. In the parent rclone-kit repository, commit the updated submodule pointer together with the Python
   or build changes that consume it.
4. Rclone-kit CI checks out submodules recursively and builds the exact recorded commit.

Never leave the parent pointing at a Go commit that exists only in a local worktree. CI should check
that the commit is fetchable from the configured submodule remote.

For upstream work, create the clean OAuth branch in a separate rclone worktree or switch the
submodule worktree deliberately. Do not mix integration-only ABI commits into the upstream PR.

## Go source design

### Keep the upstream change narrow

The OAuth change should separate two concepts that rclone currently couples:

- the private address on which the temporary callback server listens; and
- the public redirect URI supplied to the OAuth provider and token endpoint.

The preferred implementation puts these values in rclone's per-call configuration context so they
work through CLI flags, RC, and librclone without introducing another mutable package global. For
example, add fields equivalent to:

```text
OAuthListenAddress  = 127.0.0.1:0
OAuthRedirectURL    = https://service.example/callback
```

RC callers can pass the values through the call's `_config` object. The OAuth setup must read them
from `fs.GetConfig(ctx)`, bind the configured listener address, discover the actual address after a
`:0` bind, and use the public redirect consistently in both authorization and token exchange.

The status response should expose enough private information for a trusted wrapper to relay the
browser request—at minimum the rclone-generated internal `/auth` URL or actual listener address. It
must never make the callback target caller-controlled. Rclone continues to generate and validate
OAuth state.

Required Go tests include:

- default behavior remains the current loopback URI and fixed/default port behavior;
- a custom listen address does not change the advertised redirect;
- a custom public redirect is present in the provider authorization URL;
- token exchange uses the same custom redirect;
- `127.0.0.1:0` reports the actual bound port;
- `config/oauthstatus` reports readiness without exposing tokens;
- cancel and cleanup stop the listener; and
- invalid listen and redirect values fail before browser interaction.

### Add a downstream C bridge

Do not make Python depend directly on the four experimental upstream `Rclone*` symbols. Add a new
`package main` under the fork, for example:

```text
native/rclone/librclone/rclonekit/
├── main.go
├── abi.h
├── imports.go
├── build_info.go
└── main_test.go
```

This package uses `github.com/rclone/rclone/librclone/librclone` internally, registers the required
backends and RC methods, and exports a rclone-kit-owned ABI. Upstream is free to evolve its
experimental header; rclone-kit controls the symbols shipped in its wheels.

### C ABI version 1

Keep the ABI small, C-only, and independent of CPython. Use fixed-width C integer types and output
parameters rather than returning Go-defined or compiler-sensitive structures by value. A proposed
v1 surface is:

```c
uint32_t RcloneKitABIVersion(void);

int32_t RcloneKitBuildInfo(
    uint8_t **output,
    size_t *output_length
);

int32_t RcloneKitInitialize(
    const uint8_t *input,
    size_t input_length,
    uint8_t **output,
    size_t *output_length
);

int32_t RcloneKitRPC(
    const uint8_t *method,
    size_t method_length,
    const uint8_t *input,
    size_t input_length,
    uint8_t **output,
    size_t *output_length
);

int32_t RcloneKitFinalize(
    uint8_t **output,
    size_t *output_length
);

void RcloneKitFree(void *allocation);
```

The checked-in `abi.h`, not the generated cgo header, is the ABI contract. The cgo-generated header
is retained as a build artifact and compared to expected exports, but Python declares only the
contract above.

Contract rules:

- all input and output bytes are UTF-8 JSON except the method name;
- every non-null output allocation is released exactly once with `RcloneKitFree`;
- null input with nonzero length is rejected;
- ABI/lifecycle failures use reserved negative status codes;
- `RcloneKitRPC` returns the positive RC HTTP-like status for an executed call;
- the output contains structured JSON on every expected error path;
- initialization is allowed once per process and fixes the config path for that runtime;
- finalization is best-effort and does not authorize unloading the DLL; and
- all exported functions recover Go panics and convert them to sanitized error JSON where recovery
  is technically possible.

`RcloneKitBuildInfo` returns at least:

```json
{
  "abiVersion": 1,
  "rcloneVersion": "...",
  "rcloneCommit": "...",
  "goVersion": "go1.26.5",
  "buildTags": ["..."],
  "target": "windows/amd64"
}
```

The Python package refuses to initialize a library with an unexpected ABI version or fork commit.

### Initialization behavior

The custom initializer must improve on upstream `librclone.Initialize()` by accepting initialization
JSON before `configfile.Install()` runs. At minimum it needs:

- an absolute config path or an explicit in-memory/empty-config mode;
- log level and an optional log sink strategy;
- cache and temporary directories;
- a fixed user agent/version suffix; and
- feature flags required by the bridge.

The config path is immutable after initialization. Do not use `config/setpath` as a normal tenant
switch. One runtime owns one config domain for its entire process lifetime.

`Finalize` must stop every job, server, and mount tracked by the bridge before performing rclone's
best-effort cleanup. Because upstream finalization is incomplete and Windows cannot safely unload a
Go shared library, process exit remains the hard cleanup boundary.

### Explicit RC registration

Upstream `librclone` imports all storage backends and core operation/sync RC packages, but it does
not import all serve implementations. The downstream `imports.go` must explicitly register the
features rclone-kit promises:

- all supported storage backends;
- `fs/operations` and `fs/sync` RC methods;
- mount RC methods and the target-supported mount implementation;
- the generic serve RC controller;
- every supported serve type, initially HTTP and the types already exposed by rclone-kit; and
- any other RC package needed by the operation coverage matrix.

Avoid importing `cmd/all` merely to get side effects. It pulls in the entire CLI command tree and
obscures what the library supports. Maintain an explicit list and test it with `rc/list` or focused
capability calls.

### Mount build variants

Authorization and ordinary storage operations do not require `cmount`. The first working DLL should
be built without mount tags to reduce the proof-of-concept variables.

The production Windows library needs `-tags cmount` if rclone-kit will preserve Windows mount
support. That requires WinFsp with its Developer headers and `CPATH` configured during the build;
those headers are not currently installed on this machine. Runtime users still need a compatible
WinFsp installation. Linux mount support must be built and tested inside the declared manylinux
environment with the relevant FUSE development/runtime assumptions documented.

If one full library produces unacceptable runtime dependencies, publish two wheel extras or native
variants only after measuring the result. Do not prematurely split `core` and `mount` libraries:
multiple loaded Go runtimes and duplicate rclone global state would make the Python lifecycle harder.

## Python runtime architecture

### Package layout

Add these module boundaries:

```text
src/rclone_kit/native/
├── __init__.py
├── abi.py                 ctypes declarations and allocation ownership
├── build_info.py          manifest and BuildInfo validation
├── errors.py              ABI/loading/lifecycle errors
├── library.py             platform resolution and absolute-path loading
├── runtime.py             initialize, rpc, jobs, finalize
└── worker/
    ├── protocol.py        versioned parent/child messages
    ├── process.py         child entry point and library ownership
    └── supervisor.py      spawn, deadlines, crash handling, cleanup

src/rclone_kit/rc/
├── __init__.py
├── client.py              typed JSON RPC boundary
├── errors.py              RC status/error conversion
├── jobs.py                async job handles, wait, stop
├── operations.py          operation-level RC adapters
├── mounts.py              MountHandle backed by mount/*
└── servers.py             ServeHandle backed by serve/*

src/rclone_kit/authorization/
├── __init__.py
├── types.py               requests, results, public statuses
├── manager.py             limits, indexes, routing, ownership
├── session.py             public session lifecycle
├── state_machine.py       config/create and config/update driver
├── relay.py               safe callback forwarding
├── worker.py              authorization-specific worker commands/events
└── errors.py              sanitized authorization failures
```

`native.abi` is the only module allowed to import `ctypes`. `rc.client` receives Python bytes/mappings,
never raw pointers. Authorization depends on the RC client and worker protocol, not on `ctypes`
directly.

### Library loading

Resolve the library in this order:

1. explicit `library_path` passed by the caller;
2. a development override such as `RCLONE_KIT_LIBRARY`;
3. the platform library included in the installed wheel.

Always resolve an absolute path and validate the adjacent manifest and SHA-256 before loading. On
Windows, configure only the required package directory for dependent-DLL lookup and load the library
by absolute path. Never search the current working directory. Never call `FreeLibrary` or attempt a
hot reload.

The wheel remains `py3-none-<platform>` because the C ABI does not use the CPython extension ABI.
Python 3.13 remains the package's language floor.

### Runtime ownership

Expose an internal `RcloneRuntime` with these invariants:

- one loaded library;
- one initialization;
- one immutable config path;
- a lock around lifecycle and global-state mutations;
- tracked RC jobs, mounts, and servers;
- idempotent close; and
- no use after close.

Regular single-user library usage can own an in-process `RcloneRuntime`. A web application that needs
tenant isolation should use worker runtimes rather than switching config paths inside one runtime.

Use `_async: true` and rclone job IDs for long operations. Do not block a Python thread inside one
FFI call for transfers that need progress, cancellation, or application shutdown behavior.

### Authorization workers

Every active authorization session gets a fresh Python worker process that loads the shared library.
This is still C ABI integration: the child calls the DLL directly, but the process boundary contains
rclone's global OAuth/config state and provides reliable cleanup.

Use Python's `spawn` start method on **both Windows and Linux**. Do not fork a process after a Go
runtime has been loaded; inherited runtime threads and locks are not a supported isolation strategy.

```text
FastAPI/application process
│
├── AuthorizationManager
│   ├── session/state index
│   ├── public callback relay
│   └── worker supervisor
│
└── spawned Python authorization worker
    ├── loads librclone_kit
    ├── initializes a session-private config
    ├── starts config/create or config/update asynchronously
    ├── reports OAuth readiness and private listener address
    ├── completes token exchange inside rclone
    └── exits after returning the completed config
```

The worker protocol is versioned JSON messages over a private
`multiprocessing.Connection` or equivalent framed local IPC. Messages include `start`, `ready`,
`status`, `cancel`, `completed`, and `failed`. Tokens and completed config values are never logged.
The parent owns deadlines and forcibly terminates a worker that does not shut down after cancellation.

### Callback flow

The embedded authorization flow is:

1. The manager creates a session ID, temporary directory, deadline, and spawned worker.
2. The worker initializes the DLL with the private config path.
3. The worker starts `config/create` or `config/update` with `_async: true` and per-call `_config`
   containing the private listen address and public redirect URI.
4. The worker polls `config/oauthstatus` and reports rclone's internal `/auth` URL and OAuth state to
   the parent.
5. The parent returns an application URL such as `/rclone-auth/<session>/start`; this route forwards
   only to the listener recorded for that session.
6. Rclone's `/auth` endpoint redirects the browser to the provider using the public callback URI.
7. The provider returns to the public callback. The manager uses rclone state as an untrusted lookup
   key and forwards the raw query to the stored private listener.
8. Rclone validates state and performs the code exchange using the same public redirect URI.
9. The worker completes the config state machine and returns the completed remote configuration.
10. The parent marks the session terminal and the worker stops jobs, finalizes, and exits.

The callback target is always taken from private session state. A request parameter, header, or
callback URL must never choose the upstream address, preventing the relay from becoming an SSRF
proxy.

The first production deployment should use one authorization-manager application instance or sticky
routing to the instance that owns a session. Multi-process FastAPI workers and multiple containers do
not share Python worker handles. Scale-out requires an explicit owning-instance ID plus routed
callbacks, or a dedicated distributed session coordinator; it is a later deployment milestone, not
something an in-memory manager can claim to solve.

## Migrating existing rclone-kit operations

The complete, method-by-method execution ledger is maintained separately in
[`rclone_cli_to_c_abi_migration_plan.md`](rclone_cli_to_c_abi_migration_plan.md). That document is
normative for the operation migration and executable-removal gates. This section summarizes the
architecture only.

The current `RcloneBackend.run()` and `launch()` protocols model command lines and subprocesses. They
must not be implemented by translating arbitrary argument arrays inside the DLL. Introduce an RC
backend alongside the CLI backend and migrate at operation boundaries.

### Coverage categories

| Existing behavior | C ABI target | Notes |
| --- | --- | --- |
| version/capabilities | `core/version`, `rc/list`, `RcloneKitBuildInfo` | First smoke tests. |
| config list/read/create/update/delete | `config/*` | Config path is fixed at initialization. |
| obscure password | `core/obscure` | No subprocess needed. |
| list/stat/size | `operations/list`, `operations/stat`, `operations/size` | Validate memory behavior for very large listings. |
| copy/move/delete/purge | `operations/*` and `sync/*` | Use async jobs for long work. |
| progress/cancel | `core/stats`, `job/status`, `job/stop` | Wrap as typed `JobHandle`. |
| mount | `mount/mount`, `mount/listmounts`, `mount/unmount` | Requires build tags and `MountHandle`. |
| HTTP/WebDAV/etc. serve | `serve/start`, `serve/list`, `serve/stop` | Explicitly import each supported serve implementation. |
| generic long-running process handle | Replace with typed job/mount/server handle | `Process` is not meaningful in-process. |
| streaming diff/list output | Bridge extension or redesigned iterator | RC currently materializes many responses; measure before deciding. |
| upload/download methods needing raw HTTP request/response | Focused bridge extension | Upstream librclone rejects RC methods with `NeedsRequest`/`NeedsResponse`. |
| arbitrary user-supplied CLI arguments | No final compatibility promise | Keep explicit CLI escape hatch only during migration. |

### Streaming and raw-body gap

Do not hide the largest compatibility gap. The current ABI returns one allocated JSON buffer. Large
recursive listings, diff streams, and RC methods requiring raw HTTP bodies cannot be made scalable by
renaming them.

Before removing the executable, add a pull-based ABI extension if measurements show it is required:

```text
RcloneKitStreamOpen   -> opaque uint64 handle
RcloneKitStreamRead   -> next bounded byte chunk
RcloneKitStreamCancel -> stop producer
RcloneKitStreamClose  -> release handle
```

The Go bridge owns a bounded channel or pipe and Python pulls chunks. Do not call arbitrary Python
callbacks from Go threads. Handles are scoped to one runtime, are never reused, and are closed during
finalization. The exact stream operations should be implemented directly over the relevant rclone Go
packages, not by invoking Cobra commands repeatedly.

Create a coverage matrix for every public rclone-kit method before implementation. Each row records
its current CLI command, proposed RC method, return-type change, cancellation model, streaming needs,
and parity test. A feature cannot leave the CLI backend until its row is green on Windows and Linux.

### Public API transition

During migration:

- keep `CliRcloneBackend` as the default for unported operations;
- add `EmbeddedRcloneBackend` for completed operation-level adapters;
- make authorization use the embedded worker path from its first public release;
- introduce `JobHandle`, `MountHandle`, and `ServeHandle` before changing methods that currently
  return `Process` or `CompletedProcess`; and
- issue deprecations before removing public subprocess-shaped values.

The final major release makes the embedded backend the default and removes the executable from the
wheel only after parity, live tests, and migration documentation are complete.

## Native build process

### Local Windows prerequisites

The installed toolchain was verified at:

```text
C:\Program Files\Go\bin\go.exe        go1.26.5 windows/amd64
C:\Programs\w64devkit\bin\gcc.exe    GCC 14.2.0
```

The current Codex process inherited an older `PATH`, although the machine-level path contains Go.
A new terminal resolves `go` normally. Build scripts should still print the resolved executable and
fail clearly when the toolchain version differs from `native/toolchain.toml`.

WinFsp Developer headers were not found. They are needed only when the Windows production build adds
`-tags cmount`.

### Canonical build entry point

Use one Python orchestration command rather than platform-specific manual sequences:

```powershell
uv run python scripts/native/build.py --target windows-amd64 --profile development
```

It performs:

1. submodule and clean-worktree validation;
2. Go, compiler, target, and build-tag validation;
3. affected Go tests;
4. executable build;
5. shared-library build;
6. export/dependency inspection;
7. C and Python ABI smoke tests;
8. manifest and SHA-256 generation; and
9. output staging under `build/native/<target>/`.

For the first no-mount Windows spike, the underlying commands are equivalent to:

```powershell
$env:CGO_ENABLED = "1"
$env:CC = "C:\Programs\w64devkit\bin\gcc.exe"

Set-Location native\rclone
go test ./lib/oauthutil ./fs/config ./librclone/...

go build -trimpath -buildvcs=true `
    -o ..\..\build\native\windows-amd64\rclone.exe `
    .

go build -trimpath -buildvcs=true -buildmode=c-shared `
    -o ..\..\build\native\windows-amd64\librclone_kit.dll `
    ./librclone/rclonekit
```

The production profile adds the reviewed version `-ldflags`, resource metadata, and `cmount` tags
only after the mount toolchain is installed and tested. The build script—not this illustrative shell
fragment—is the source of truth.

The executable is built from the same commit for:

- manual OAuth testing;
- upstream issue/PR reproduction;
- command-versus-RC differential tests;
- identifying rclone's embedded source version; and
- emergency diagnostics during migration.

It is not the final production call path.

### Linux build

Build the Linux shared library and wheel in a pinned manylinux2014-compatible container rather than
on `ubuntu-latest` directly. The current wheel declares `manylinux2014_x86_64`, which represents a
glibc 2.17 compatibility baseline. A cgo library built on a newer host can acquire newer glibc symbol
requirements while still being incorrectly labeled as manylinux2014.

The container build must:

- install the pinned Go 1.26.5 toolchain;
- use a compiler available inside the manylinux image;
- build the executable and `.so` from the same submodule commit;
- run the C/Python smoke tests inside the container;
- inspect the library with `auditwheel show` and ELF tools;
- build the wheel inside the same environment; and
- install and run the wheel in clean glibc-baseline and modern Linux containers.

Do not use `auditwheel repair` as a substitute for understanding dependencies. Record what it changes
and ensure rclone/Go runtime libraries are not duplicated or relocated incorrectly.

### Build outputs

Each target produces an untracked directory like:

```text
build/native/windows-amd64/
├── rclone.exe
├── librclone_kit.dll
├── rclonekit_abi.h
├── native-manifest.json
├── SHA256SUMS
├── RCLONE_LICENSE
└── smoke-results.json
```

The manifest records:

- rclone-kit version and C ABI version;
- fork URL, fork commit, upstream base version/commit;
- whether the worktree was clean;
- Go version and relevant `go env` values;
- C compiler identity;
- target, build tags, and link flags;
- exported symbol list;
- runtime native dependencies;
- hashes and sizes of every output; and
- smoke-test versions/results.

Do not promise byte-for-byte reproducibility until two clean builders reproduce the same cgo output.
The immediate reproducibility guarantee is source/toolchain/configuration provenance plus verified
artifact handoff. Add a reproducibility CI experiment and strengthen the guarantee if results support
it.

## Wheel and release pipeline

### Transitional wheel contents

During migration, a platform wheel contains:

```text
rclone_kit/assets/native/<wheel-platform-tag>/
├── librclone_kit.dll or librclone_kit.so
├── native-manifest.json
├── librclone_kit.dll.sha256 or librclone_kit.so.sha256
└── RCLONE_LICENSE

rclone_kit/assets/rclone/<wheel-platform-tag>/
└── current executable files             temporary migration fallback
```

The final C-ABI-only release removes the second tree. Generated artifacts remain absent from tracked
`src/`; the canonical build continues to copy them into a temporary wheel source tree, preserving the
project's existing clean-source build design.

### Changes to existing build code

Replace the executable-only `RcloneArtifact` model with separate concepts:

- `NativeSource`: fork/submodule/toolchain identity;
- `NativeTarget`: OS, architecture, wheel tag, build tags, library filename, executable filename;
- `BuiltNativeBundle`: paths and generated manifest for one completed build; and
- `PackagedNativeBundle`: the subset placed in a wheel.

Update:

- `scripts/prepare_rclone_artifact.py` into a migration-compatible native staging layer;
- `scripts/build_distribution.py` to build or accept a verified `--native-dir`;
- `_build_backend.py` comments and tag verification to describe a C shared library;
- `scripts/verify_distribution.py` to require the library, manifest, hash, license, exact exports,
  and correct platform tag;
- `scripts/smoke_test_installed_wheel.py` to load the packaged library and call BuildInfo,
  initialization, and `core/version`; and
- `runtime/platform.py` into the new native source/target model.

A local developer may reuse `build/native/<target>` with `--native-dir`; CI release jobs always start
from a clean submodule and create the bundle themselves.

### CI jobs

Add these gates:

1. **Python quality:** current Ruff, Pyright, and unit tests.
2. **Rclone patch tests:** focused Go tests on Windows and Linux.
3. **Rclone quicktest:** `make quicktest` or its exact equivalent on the integration commit.
4. **Native Windows:** build executable/DLL, inspect dependencies, run C/Python/native tests.
5. **Native Linux:** build in manylinux, inspect/validate ELF compatibility, run tests.
6. **Parity:** run migrated operations through the executable and C ABI against local/memory
   backends and compare domain results.
7. **Wheel:** stage that job's native bundle, build, verify, install in a clean environment, smoke.
8. **Authorization offline:** real library plus fake OAuth authorization/token endpoints and relay.
9. **Release assembly:** require both platform wheels, manifests, hashes, test summaries, and native
   diagnostic bundles.
10. **Live protected tests:** selected Google Drive flow and normal storage regressions using secrets;
    never run for untrusted pull requests.

Use the current official `actions/setup-go` major version and set `cache-dependency-path` to the
submodule's `go.sum`. Pin GitHub Actions by commit as the existing workflows do. Cache Go modules and
build output, but never treat a cache hit as a produced release artifact.

### Release artifacts and provenance

Publish platform wheels to PyPI. Attach these additional files to the matching GitHub release:

- the normal rclone executable for each supported target;
- the shared library and ABI header;
- native manifests and SHA-256 files;
- the exact fork commit/source archive reference;
- licenses; and
- native and authorization smoke summaries.

The installed wheel verifies its library against its packaged manifest before the first load. Release
assembly verifies that the bytes in each wheel are identical to the native bundle produced and tested
by the platform job.

## Test plan

### Go tests in the fork

- listener/public redirect behavior and backward compatibility;
- redirect consistency at authorization and token endpoints;
- port-zero discovery and listener cleanup;
- RC OAuth status/stop behavior;
- ABI version and build info;
- initialize-once and invalid initialization input;
- null pointers, invalid UTF-8/JSON, empty calls, and allocation cleanup;
- panic-to-error conversion;
- required RC method and serve/mount capability registration; and
- finalization of tracked jobs/resources where supported.

Run the affected package tests first, then rclone `quicktest`. The rclone contribution rules require
the submitter to understand, build, and actually test AI-assisted changes before opening a PR.

### Python unit tests

- every `ctypes` signature and pointer ownership path;
- output freeing on success, RC error, JSON error, and Python decode failure;
- absolute-path loading and manifest/hash rejection;
- ABI and fork-commit mismatch;
- runtime lifecycle and use-after-close;
- RC error mapping and redaction;
- async job poll, timeout, cancel, and crash behavior;
- worker protocol framing and version mismatch;
- spawn-only worker behavior;
- authorization lifecycle, capacity, expiry, and idempotent cleanup;
- callback raw-query preservation, header filtering, replay rejection, and SSRF prevention; and
- no secrets in logs/exceptions.

Use fake ABI objects for fast unit tests. Actual DLL/SO loading belongs in native integration tests.

### Native integration tests

- repeated `core/version` and `rc/list` calls;
- memory and local backend CRUD;
- synchronous and asynchronous transfer;
- concurrent read-only RC calls and serialized state mutation;
- deliberate worker crash and parent cleanup;
- config isolation in two spawned workers;
- mount and unmount on supported privileged runners;
- start/list/stop HTTP serve;
- long-running resource shutdown; and
- load/install test on a clean machine without Go or GCC.

### Differential parity tests

For each migrated public operation, run the existing CLI backend and new embedded backend against the
same local/memory fixture and compare normalized domain results, filesystem effects, errors, and
cancellation behavior. Do not compare incidental logs or subprocess return types.

### Authorization tests

Build an offline fake OAuth provider and use a real library worker:

1. start an authorization session;
2. get the public start link;
3. relay to rclone's private `/auth` endpoint;
4. follow the provider redirect;
5. return code and state through the public callback;
6. assert rclone sends the public redirect URI to the token endpoint;
7. assert Python never performs the token request;
8. receive the completed config;
9. use the configured remote against a controlled test backend where possible; and
10. verify worker, listener, jobs, and temporary files are gone.

Repeat with simultaneous sessions whose callbacks complete out of order. Test denial, state mismatch,
duplicate callback, token failure, timeout, worker crash, relay failure, and application shutdown.

## Delivery phases and pull-request sequence

### Phase 0 — establish source ownership

1. Create/configure the public rclone fork.
2. Create the clean upstream and downstream integration branches.
3. Move the production checkout to `native/rclone` as a submodule.
4. Add `native/toolchain.toml`, native README, ignore rules, and clean-submodule checks.
5. Keep `reference/rclone` out of production scripts.

Exit gate: a clean clone with submodules can identify the exact rclone source and Go toolchain without
using the reference directory.

### Phase 1 — OAuth patch in rclone

1. Implement context-scoped listen address and public redirect URI.
2. Support port zero and report the actual private listener endpoint.
3. Extend OAuth status as narrowly as required by the relay.
4. Add focused Go tests and run quicktest.
5. Prove the normal rclone executable flow manually through a small relay.
6. Prepare the clean upstream contribution.

Exit gate: the forked executable completes the external callback flow and preserves default behavior.

### Phase 2 — custom ABI and native proof

1. Add `librclone/rclonekit` and ABI v1.
2. Add fixed-config initialization and build info.
3. Add explicit RC registrations.
4. Build `rclone.exe` and `librclone_kit.dll` locally with Go 1.26.5/GCC 14.2.
5. Add C and Python `core/version` smoke tests.
6. Prove allocation/free and repeated call stability.

Exit gate: one command produces both binaries and actual Python can initialize, call, free, and close
the DLL without using upstream's Python wrapper.

### Phase 3 — build and packaging foundation

1. Add native source/target/bundle models.
2. Implement build, inspect, manifest, hash, and smoke scripts.
3. Add Windows CI and pinned manylinux container build.
4. Stage the library into transitional wheels alongside the executable.
5. Extend wheel verification and installed-wheel smoke tests.

Exit gate: clean Windows and Linux CI runners build, package, install, and call the exact library in
their wheels.

### Phase 4 — Python ABI and RC runtime

1. Implement the private `ctypes` binding.
2. Implement library resolution and manifest validation.
3. Implement `RcloneRuntime`, typed RC errors, async `JobHandle`, and cleanup.
4. Port config, version, and a memory-backend CRUD vertical slice.
5. Add CLI-versus-ABI parity tests.

Exit gate: an opt-in embedded backend passes the vertical-slice tests on both platforms.

### Phase 5 — embedded authorization

1. Implement spawn worker protocol and supervisor.
2. Implement the non-interactive rclone config state-machine driver.
3. Implement session manager, safe relay, deadlines, cancellation, and redaction.
4. Add fake-provider end-to-end and concurrency tests.
5. Add one protected live Google Drive test.
6. Document FastAPI integration and single-instance/sticky-routing deployment constraints.

Exit gate: remote users can authorize through a public link, rclone exchanges the code inside an
isolated DLL worker, and rclone-kit returns a usable config without exposing token mechanics.

### Phase 6 — operation migration

Execute this phase through the row IDs, migration waves, parity requirements, public compatibility
rules, and removal gates in
[`rclone_cli_to_c_abi_migration_plan.md`](rclone_cli_to_c_abi_migration_plan.md). A category below is
not complete until all of its ledger rows are complete on Windows and Linux.

Port in this order:

1. configuration and metadata;
2. listing/stat/size;
3. simple file operations;
4. sync/copy and job progress;
5. serve handles;
6. mount handles and platform tags;
7. diff/large-output streaming; and
8. remaining public escape hatches.

Every operation needs a coverage-matrix row, typed return behavior, unit tests, native tests, and
CLI differential tests before changing the default path.

Exit gate: all supported public behavior has an embedded implementation or is explicitly deprecated.

### Phase 7 — C-ABI-only product release

1. Make the embedded backend the default for a full release while retaining an explicit CLI fallback.
2. Collect crash, dependency, performance, and compatibility evidence.
3. Deprecate executable-shaped return types and generic CLI execution.
4. In the next major release, remove the executable from wheels when all acceptance gates pass.
5. Continue building the executable as a CI/test/GitHub diagnostic artifact.

Exit gate: installed wheels use only the shared library in normal production paths.

## Work breakdown by repository

### Rclone fork changes

- OAuth private-listener/public-redirect configuration;
- port-zero listener discovery;
- structured status required by the relay;
- upstream-quality OAuth tests;
- downstream C ABI bridge and canonical `abi.h`;
- initialization/build-info improvements;
- explicit RC/serve/mount imports;
- C harness and bridge tests; and
- downstream version/build metadata.

### Rclone-kit changes

- submodule and native toolchain manifest;
- native build/inspection/manifest scripts;
- platform library artifact model and resolver;
- wheel staging, verification, and release changes;
- private `ctypes` binding and memory safety;
- runtime/RC/job abstractions;
- spawned worker infrastructure;
- authorization session, state machine, and relay;
- operation-by-operation backend migration;
- typed resource handles;
- native, parity, authorization, and live tests; and
- development, deployment, and release documentation.

## Risks and controls

| Risk | Control |
| --- | --- |
| Experimental upstream ABI changes | Ship a rclone-kit-owned versioned ABI. |
| Go runtime cannot be unloaded | Load once; use process exit for hard cleanup. |
| Rclone global config/OAuth state | Immutable runtime config and one spawned worker per auth session. |
| FastAPI worker/pod callback lands elsewhere | Initially require one authorizer instance or sticky routing; design distributed ownership separately. |
| Native crash takes down Python | Authorization is isolated; offer worker runtimes for high-isolation deployments. |
| C allocation leak or double free | One private ABI module, `finally`-based freeing, native stress tests. |
| DLL search-path hijacking | Absolute path, verified hash/manifest, restricted dependency directory. |
| Incorrect manylinux tag | Build at policy baseline and validate with auditwheel/clean containers. |
| Windows mount build complexity | Stage no-mount proof first; add WinFsp Developer headers and dedicated mount CI later. |
| RC cannot represent current streaming/raw-body operations | Coverage matrix and pull-based bridge extension before CLI removal. |
| Fork drifts far from upstream | Minimal clean patch, release-based integration branch, scheduled upstream sync. |
| Source and wheel artifact diverge | Submodule pin, generated manifest, same-workflow build/test/package, byte comparison at release assembly. |
| Secrets leak through JSON/logging | Typed redaction at ABI, worker, RC, authorization, and exception boundaries. |

## Definition of complete

The C ABI transition is complete only when:

1. The wheel contains a manifest-verified library built from its recorded fork commit.
2. Python never depends on upstream's experimental ABI directly.
3. Every allocation and lifecycle path has native tests.
4. Authorization works through a public link while the listener stays private.
5. Rclone performs provider authorization and token exchange.
6. Concurrent authorization sessions use isolated spawned workers.
7. Config paths are immutable within a runtime and tenant configs are not switched globally.
8. All public operations have documented RC/bridge mappings and parity tests.
9. Mount, serve, jobs, cancellation, streaming, and shutdown have typed non-subprocess models.
10. Windows and Linux wheels load on clean systems without build tools.
11. The executable and library are built from the same source for every release test cycle.
12. The executable can be removed from installed wheels without losing a supported public feature.

## Immediate next implementation steps

The first development sequence should be:

1. Create the maintained fork and branch model.
2. Add it at `native/rclone` as a submodule; leave `reference/rclone` untouched for research.
3. Implement and test the context-scoped OAuth listener/redirect change in the clean fork branch.
4. Cherry-pick that commit into the integration branch.
5. Add `librclone/rclonekit` with ABI version, BuildInfo, Initialize, RPC, Finalize, and Free.
6. Build the no-mount Windows DLL with the installed Go 1.26.5 and GCC 14.2.
7. Add a minimal Python binding and pass `core/version` plus memory-backend smoke tests.
8. Only then implement the native build scripts and wheel staging around the proven artifact.

Do not begin by rewriting all existing operations. The OAuth patch plus a complete C-ABI vertical
slice—source pin, build, load, call, free, test, and package—will validate the irreversible structural
choices before the larger migration starts.

## References

- [Rclone librclone documentation](https://github.com/rclone/rclone/blob/master/librclone/README.md)
- [Rclone contribution guide](https://github.com/rclone/rclone/blob/master/CONTRIBUTING.md)
- [Go `c-shared` build mode](https://pkg.go.dev/cmd/go#hdr-Build_modes)
- [Go cgo documentation](https://pkg.go.dev/cmd/cgo)
- [Python wheel platform compatibility tags](https://packaging.python.org/en/latest/specifications/platform-compatibility-tags/)
- [GitHub `setup-go`](https://github.com/actions/setup-go)
- [`rclone_remote_oauth_upstream_change.md`](rclone_remote_oauth_upstream_change.md)
- [`rclone_c_shared_library_investigation.md`](rclone_c_shared_library_investigation.md)
- [`rclone_cli_to_c_abi_migration_plan.md`](rclone_cli_to_c_abi_migration_plan.md)
- [`docs/rclone_authorization_design.md`](../docs/rclone_authorization_design.md)
