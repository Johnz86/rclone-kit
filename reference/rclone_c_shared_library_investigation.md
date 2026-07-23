# Embedding rclone as a Go C-shared library

Status: investigation and implementation recommendation  
Date: 2026-07-23  
Investigated rclone source: `v1.74.0-289-g2f3895fa3`

## Executive conclusion

Building rclone as a shared library is technically viable and is much less of a greenfield project than it first appears. Rclone already contains an experimental `librclone` C ABI, a Python `ctypes` example, and C smoke tests. The library exposes rclone's JSON RC API through four C functions. We do not need to design a broad C binding to every rclone operation.

The shared-library approach does **not**, by itself, solve remote OAuth authorization. Embedded rclone still starts its temporary OAuth callback HTTP listener and still supplies a loopback redirect URI unless rclone receives the same public-redirect/private-listener change described in [rclone_remote_oauth_upstream_change.md](rclone_remote_oauth_upstream_change.md). A callback relay is still required when the Python application runs behind FastAPI, a container, or another service boundary.

For rclone-kit, the important trade-off is isolation:

- A shared library removes a subprocess boundary and the `rclone rcd` HTTP control surface.
- Rclone configuration, jobs, caches, logging, and OAuth coordination are process-global. One DLL loaded into one Python process is not naturally partitioned by user or authorization session.
- The DLL must remain loaded until the host exits on Windows, and `RcloneFinalize` does not currently cancel outstanding asynchronous jobs or fully reset rclone.
- A panic is converted into an RC error in the normal RPC path, but native/runtime failures can still affect the Python host process.

Therefore the recommended near-term authorization implementation remains a patched rclone executable with one process per authorization session. `librclone` is worth prototyping as a separate, optional backend for repeated steady-state RC operations, where avoiding process startup and HTTP serialization has greater value. It should not replace the executable backend until its lifecycle, concurrency, packaging, and command-coverage limits are proven.

## What rclone already provides

Rclone's [`librclone` directory](https://github.com/rclone/rclone/tree/master/librclone) is specifically intended to build rclone as a C shared or archive library. The upstream documentation calls the interface experimental and says that it may change. Its shim is intentionally small and delegates to the RC API rather than exporting each storage operation separately.

The C ABI exported from `reference/rclone/librclone/librclone.go` is:

```c
void RcloneInitialize(void);
void RcloneFinalize(void);

struct RcloneRPCResult {
    char *Output;
    int Status;
};

struct RcloneRPCResult RcloneRPC(char *method, char *input);
void RcloneFreeString(char *str);
```

`RcloneRPC` accepts an RC method name such as `core/version`, `config/create`, `job/status`, or `sync/copy`, plus a JSON object. It returns a JSON string and an HTTP-like integer status. The caller owns the output string and must release it with `RcloneFreeString`, never Python's or another C runtime's `free`.

The existing Python example at `reference/rclone/librclone/python/rclone.py` already demonstrates the essential `ctypes` declarations. It is a useful reference, but it is not production packaging: upstream says the Python wrapper still needs expansion and publication.

The wrapper does not expose every CLI command. Internally it looks up an RC call and invokes it in-process. Calls that require direct access to a raw HTTP request or response cannot be used through this ABI. The source currently identifies examples such as `operations/uploadfile` and `core/command`. This means `librclone` is an RC backend, not a drop-in implementation of rclone-kit's current generic `run(args)` and `launch(args)` command interface.

## What embedding changes for authorization

### Benefits

With the library loaded, Python can call `config/create`, `config/update`, `config/oauthstatus`, `job/status`, and `job/stop` directly as JSON RPCs. This removes:

- launching and supervising an `rclone rcd` control server;
- allocating and protecting an RC TCP port;
- RC HTTP authentication and HTTP request serialization;
- parsing an interactive CLI's standard output to discover authorization state;
- process-start overhead for each ordinary RC request.

The API remains rclone-owned. Python coordinates the session but does not implement provider-specific OAuth exchange or token refresh logic.

### What it does not solve

`config/create` and `config/update` eventually enter the same rclone backend configuration and OAuth code as the executable. That code starts a temporary local callback server. Embedding changes how Python calls rclone; it does not make the provider able to reach that listener.

The desired flow still requires all of the following:

1. Rclone listens on an internal address reachable by the callback relay, for example `0.0.0.0:<private-port>` inside an isolated worker/container network.
2. Rclone puts an externally reachable HTTPS callback URL in the provider authorization request.
3. The relay routes that public callback to the exact rclone listener for the session.
4. Rclone validates OAuth state and exchanges the returned code itself.

Merely rewriting the browser-visible authorization URL after rclone has built it is not generally sufficient. The provider validates the `redirect_uri`, and rclone commonly sends that value again during the token exchange. Both sides must consistently use the public URI. The narrow rclone change should therefore separate the listener address from the advertised redirect URI rather than make Python rewrite protocol messages.

### Candidate embedded authorization sequence

Assuming the listener/redirect patch is present, an embedded sequence could be:

1. Load the platform library from an absolute package path and retain it for the life of the Python process.
2. Call `RcloneInitialize` once.
3. Select the rclone config file before starting work. Rclone has `config/setpath`, but the selected path is process-global.
4. Call `config/create` or `config/update` with `nonInteractive` configuration and `_async: true` where the RC call supports it.
5. Drive rclone's configuration state machine and poll `config/oauthstatus` until the authorization URL is available.
6. Return the public authorization URL to the rclone-kit caller.
7. Let the public callback relay forward the provider callback to the temporary rclone listener.
8. Poll `job/status` and the configuration result until completion. On cancellation, invoke `job/stop` and/or `config/oauthstop` as appropriate.
9. Read the completed remote configuration through an RC method and persist it in the selected config file.

The exact RC request/response sequence needs an executable integration test against at least one OAuth backend. Configuration is a state machine and may require repeated `config/...` continuation calls; it should not be represented as a single blocking FFI call in rclone-kit's public API.

## Concurrency and lifecycle constraints

These constraints are more consequential for authorization than the mechanics of compiling a DLL.

### Process-global rclone state

`librclone.Initialize` installs rclone's global configuration system and starts global logging/accounting. The config path, RC job registry, backend registrations, caches, and other package state live in the host process. The current OAuth status implementation also uses package-level coordination rather than a per-session object.

Consequences:

- Treat one loaded library as one rclone runtime, not as a factory for isolated clients.
- Changing the config path for one request can affect another request.
- Concurrent authorization sessions can overwrite or observe shared OAuth status unless rclone is refactored to give every configuration run an explicit session identity.
- A Python lock can serialize access, but serialization is not tenant isolation and limits the service to one active authorization flow per process.

Strong isolation requires either one OS worker process per authorization session or a substantial upstream refactor of rclone's global config/OAuth/job state. A worker that loads the DLL and is terminated after authorization is feasible, but it restores a process boundary and offers little practical advantage over invoking the rclone executable directly.

### Initialization and shutdown

The library should be initialized exactly once in a Python process. Upstream documents that asynchronous jobs must currently be cancelled manually before finalization. The internal `Finalize` implementation contains a cleanup TODO and presently performs only limited cleanup.

On Windows, upstream explicitly warns not to unload `librclone.dll` with `FreeLibrary`: the embedded Go runtime has garbage collection and background threads that do not support unloading safely. A Python application cannot reliably load a new rclone build or reset all rclone state in the same process. Process exit is the dependable cleanup boundary.

### Error boundary

`RcloneRPC` recovers ordinary Go panics around an RC call and serializes an error response. That is useful, but it is not equivalent to subprocess fault isolation. A defect in cgo, the Go runtime, native libraries, or unsafe code can terminate or corrupt the Python service. This risk should be accepted deliberately if the library is loaded into a web application's main process.

## Local build-tool inventory

No tools were installed and no rclone library build was attempted during this investigation.

| Tool | Local state | Relevance |
| --- | --- | --- |
| Go | **Not found** | Required; this is the main missing prerequisite. |
| GCC / `cc` | `C:\Programs\w64devkit\bin\gcc.exe`, GCC 14.2.0 | Suitable GCC-compatible compiler for Windows cgo. |
| GNU Make | `C:\Programs\w64devkit\bin\make.exe` | Present, although the basic library build does not require Make. |
| Clang | `C:\Programs\LLVM\bin\clang.exe`, 20.1.0, MSVC target | Present; the known MinGW GCC path is the safer first cgo configuration here. |
| CMake | Present | Not required for the basic library. |
| Zig | Present | Not required. It should not be introduced into the first build path. |
| Visual C++ `cl`, `link`, `dumpbin` | Not found on `PATH` | Not required when building with MinGW GCC and loading with `ctypes`. |
| Python | CPython 3.12.6, 64-bit AMD64 | Suitable for a local ABI smoke test; the project may test other supported versions in CI. |
| `uv` | Present | Suitable for running the Python test environment. |

The checked-out rclone module declares Go 1.25.0 in `go.mod`. Its current build workflow primarily selects Go 1.26.5 and has a Go 1.25.12 compatibility job. As of this investigation, Go's official package documentation also identifies Go 1.26.5 as the current toolchain version. Installing Go 1.26.5 is the least surprising choice because it matches rclone's primary CI. A current 1.25 patch release should satisfy the module declaration but is not the preferred reproduction environment.

The first build will need network access to download the module graph. The rclone workflow comments indicate that the module cache can be several hundred MiB. Allow comfortable temporary disk space for Go itself, modules, compilation cache, the DLL, and wheel staging; 1–3 GiB is a sensible working allowance, not a measured product footprint.

## Reproducible proof-of-concept build

### 1. Install Go

Use the official 64-bit Windows Go installer from [go.dev/doc/install](https://go.dev/doc/install), then open a new terminal and verify:

```powershell
go version
go env GOOS GOARCH CGO_ENABLED CC
```

Expected platform values on this machine are `windows`, `amd64`, and a 64-bit toolchain. No separate Visual Studio installation is required for the proposed MinGW build.

### 2. Build from the local rclone checkout

Run from `reference/rclone`. Building `./librclone` is important: it uses the checked-out and eventually patched source, rather than resolving an unrelated version by module path.

```powershell
Set-Location reference\rclone
New-Item -ItemType Directory -Force build\librclone

$env:CGO_ENABLED = "1"
$env:CC = "C:\Programs\w64devkit\bin\gcc.exe"

go mod download
go build -trimpath -buildmode=c-shared `
    -o build\librclone\librclone.dll `
    ./librclone
```

This should produce `librclone.dll` and `librclone.h`. `-ldflags=-s` can be evaluated after debugging and symbol inspection are complete; upstream says it materially reduces library size. Do not enable `-tags cmount` for the authorization/RC use case. Mount support adds WinFsp development requirements on Windows and is unrelated to OAuth or normal remote operations.

No source patch should be diagnosed from a build failure until `go env CC`, `gcc --version`, and the compiler architecture have been captured. Also record the exact rclone commit and `go version` with each produced artifact.

### 3. Smoke-test the ABI from Python

Adapt the existing upstream `librclone/python/rclone.py` wrapper to load the DLL by absolute path. Use `ctypes.CDLL`, define the returned structure exactly, set all `argtypes` and `restype` declarations before the first call, and guarantee `RcloneFreeString` in a `finally` block.

The first test should perform only:

1. load DLL;
2. initialize;
3. call `core/version` with `{}`;
4. decode the UTF-8 JSON;
5. free the output string;
6. finalize only after every job is stopped.

Do not explicitly unload the DLL. Then add an RC call against the memory backend, followed by config-file and OAuth-status calls. A provider authorization test comes only after the listener/redirect patch is applied.

### 4. Audit the Windows artifact

Use the MinGW tools to list exports and imported DLLs, and run the smoke test on a clean Windows CI runner that does not have w64devkit installed. This determines whether any compiler runtime DLL must be packaged alongside `librclone.dll`; it should be measured from the real artifact rather than assumed.

### 5. Build Linux natively at the supported compatibility baseline

The basic Linux command is:

```sh
CGO_ENABLED=1 go build -trimpath -buildmode=c-shared \
    -o build/librclone/librclone.so ./librclone
```

A production Linux wheel must not build the `.so` casually on the newest Ubuntu image. cgo links against the C library available in the build environment, so a new glibc can make the result unusable on older supported systems. Build inside the same manylinux baseline promised by the wheel, inspect with `auditwheel show`, and execute the wheel in clean containers representing the oldest and newest supported distributions. The shared library can depend on `libdl` and `libpthread` on systems where those facilities are not integrated into libc.

Native Windows and native Linux CI jobs are recommended. Cross-compiling a cgo library requires a target C cross-compiler and complicates dependency/ABI validation without helping authorization design.

## Required rclone-kit changes

### Native artifact model

The current runtime artifact and resolver code is executable-oriented: it models executable names, selects or downloads rclone archives, and returns a path that can be launched. Supporting `librclone` requires an additional artifact type and resolver, including:

- platform and architecture mapping for `.dll` and `.so` files;
- the pinned rclone source commit, Go version, C compiler version, and build flags;
- SHA-256 verification and staged extraction equivalent to the executable path;
- a wheel/package data location that can be resolved to an absolute path;
- dependency inspection and clean-machine load tests;
- license and source/build provenance alongside the native artifact;
- native CI production of Windows and manylinux-compatible libraries.

The generated header is useful as build evidence and for C tests but is not needed at Python runtime. In particular, upstream advises Windows consumers not to include the generated header across an MSVC/MinGW boundary; the Python wrapper should declare the small ABI directly.

### Python FFI layer

Create a small private module responsible for exactly one job: safe ownership of the C ABI. It should provide:

- singleton load/initialize semantics;
- an immutable absolute library path after first load;
- UTF-8 JSON encoding and decoding;
- exact `ctypes` signatures and result structure layout;
- unconditional output deallocation through `RcloneFreeString`;
- JSON/status-to-Python exception conversion with sensitive-field redaction;
- a process-wide lock around state-changing config and authorization calls;
- explicit job tracking and cancellation before shutdown;
- no attempt to unload/reload the library.

Keep raw pointers and `ctypes` objects private. The rest of rclone-kit should receive Python mappings and typed domain results.

### Backend design

Do not make the first implementation pretend that RC is a generic CLI. The current `RcloneBackend.run()` / `launch()` abstraction accepts command-line arguments and subprocess lifecycle behavior that many RC calls cannot reproduce directly.

Prefer one of these designs:

1. Add a separate `RcloneRcBackend` protocol exposing `rpc(method, params)` and job operations; or
2. Add operation-level interfaces that both CLI and RC adapters implement for explicitly supported operations.

Authorization can initially depend on the narrower RC interface without migrating unrelated commands. Keep the executable resolver and subprocess backend available during the experiment and for operations with no equivalent RC method.

### Authorization integration

The embedded implementation still needs the patched rclone source and callback-relay design. The build pipeline should produce the executable and shared library from the same pinned fork commit so behavior does not drift.

The authorization manager must explicitly choose a concurrency policy:

- **Serialized in-process:** simplest prototype, but only one active config/OAuth flow per Python process and no strong tenant isolation.
- **One embedded worker process per session:** strong isolation and dependable cleanup, but almost no operational advantage over a patched executable.
- **Refactor rclone for session-scoped state:** best theoretical embedded model, but much larger than the narrow redirect/listener contribution and unlikely to be a good first upstream proposal.

For a web-service deployment, loading a globally stateful experimental library directly into every FastAPI worker also means each worker has independent in-memory jobs while potentially sharing a config file. Worker routing and config locking must be designed deliberately; normal load balancing will not return a status poll to the worker that owns the job unless the session is routed or stored accordingly.

## Test and release gates

A proof of concept is successful only after all of these pass:

1. `core/version` works repeatedly without leaks or crashes.
2. A memory-backend operation works synchronously and asynchronously.
3. Every returned string is freed, including malformed input and error paths.
4. Status codes and rclone JSON errors become stable Python exceptions.
5. Config path selection and config read/write behavior are characterized.
6. Job cancellation is verified before finalization/process exit.
7. Two attempted concurrent config flows demonstrate and document the chosen serialization behavior.
8. A patched OAuth backend completes through the real public callback relay.
9. Cancellation, timeout, state mismatch, duplicate callback, and provider denial are tested.
10. Windows artifacts load on a clean runner with no build toolchain installed.
11. Linux artifacts load at the declared oldest glibc/manylinux baseline.
12. Wheels pass the existing distribution verification plus native-library dependency inspection.

Rclone's own CI currently exercises the librclone C and Python tests primarily on Linux, while ordinary Windows builds commonly disable cgo. Rclone-kit should therefore own Windows shared-library CI rather than assume upstream's executable jobs validate this artifact.

## Complexity estimate

These are rough engineering ranges after the Go toolchain is available, not delivery commitments.

| Scope | Complexity | Indicative effort | Main uncertainty |
| --- | --- | --- | --- |
| Build DLL locally and call `core/version` from Python | Low | 0.5–1 day | Toolchain and runtime DLL audit |
| Production Windows and Linux artifacts, FFI wrapper, checksums, wheels, clean-runner tests | Medium | 3–7 working days | manylinux compatibility and release integration |
| Embedded authorization with patched redirect/listener, RC state driver, relay, cancellation, and provider integration tests | Medium-high | 5–10 working days | backend configuration state behavior and failure handling |
| Safe concurrent multi-user auth in one Python process | High | Several weeks or more | Refactoring rclone's process-global state and upstream acceptability |
| Replacing the general CLI backend with librclone | High and open-ended | Not recommended as the first milestone | RC does not cover arbitrary CLI semantics |

The authorization estimate is not additive if the callback relay and rclone patch are already implemented for the executable. Much of that application-level work can be reused; the extra work is the FFI lifecycle and RC state driver.

## Recommended path

### Near term: ship authorization with the patched executable

Use one rclone process per authorization session, with the narrow public-redirect/private-listener patch and the public callback relay. This model naturally isolates rclone's global state, supports cancellation by terminating the process tree, and prevents native failures from taking down the FastAPI process. It also aligns with rclone-kit's current executable artifacts and backend abstractions.

Build the custom executable reproducibly from the pinned fork while the upstream contribution proceeds. This temporary fork is required for the redirect behavior whether the executable or shared library is selected.

### In parallel: run a bounded librclone spike

After installing Go, spend at most a few days on a separate prototype:

1. Build unmodified `librclone.dll` from the local checkout with the existing GCC.
2. Implement a minimal private `ctypes` wrapper.
3. Verify `core/version`, memory operations, async jobs, config path behavior, and cancellation.
4. Build the same patched source as both `rclone.exe` and `librclone.dll`.
5. Complete one serialized authorization flow through the callback relay.
6. Measure artifact size, load/start latency, memory, runtime dependencies, and cleanup behavior.

Stop the experiment if the only workable production architecture is one DLL-hosting worker process per authorization session. In that case, the executable already provides a simpler and better-supported process payload.

### Longer term: introduce librclone only where it pays for itself

If the spike is stable, retain it as an optional RC backend for high-volume, repeated storage calls. Keep authorization and unsupported CLI commands on the executable backend initially. The hybrid design lets rclone-kit benefit from direct in-process RC calls without making a globally stateful experimental ABI the sole operational path.

Do not combine the small upstream OAuth redirect/listener proposal with a session-global-state or librclone-stability redesign. Rclone's contribution guidance asks for minimal, tested changes and places a high value on backward compatibility. The narrow OAuth change has a clearer business case and review surface; library hardening can be proposed separately with measurements and focused tests.

## Primary references

- [Rclone `librclone` documentation](https://github.com/rclone/rclone/blob/master/librclone/README.md)
- [Rclone exported C shim](https://github.com/rclone/rclone/blob/master/librclone/librclone.go)
- [Rclone internal RPC implementation](https://github.com/rclone/rclone/blob/master/librclone/librclone/librclone.go)
- [Rclone Python `ctypes` example](https://github.com/rclone/rclone/blob/master/librclone/python/rclone.py)
- [Go build modes](https://pkg.go.dev/cmd/go#hdr-Build_modes)
- [Go cgo command documentation](https://pkg.go.dev/cmd/cgo)
- [Go cgo wiki, including Windows compiler requirements](https://go.dev/wiki/cgo)
- [Official Go installation instructions](https://go.dev/doc/install)
- [Rclone contribution instructions](https://github.com/rclone/rclone/blob/master/CONTRIBUTING.md)
