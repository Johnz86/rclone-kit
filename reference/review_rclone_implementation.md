The embedded path is a solid vertical implementation, but it is not yet safe to make it the only production path. The largest gaps are runtime ownership, authorization, lifecycle reliability, Linux correctness, and release validation.

I only read code and repository metadata. I did not modify files, build artifacts, or run tests.

## Critical findings

1. The packaged product and default API currently disagree

The wheel now excludes the rclone executable, but `Rclone` still defaults to `execution="cli"` and attempts executable resolution. The README and every console application also construct the CLI default.

- Default execution remains CLI in [client.py](D:/GIT/python/rclone-kit/src/rclone_kit/client.py:209).
- The constructor creates `CliRcloneBackend` unless embedded is explicitly selected in [client.py](D:/GIT/python/rclone-kit/src/rclone_kit/client.py:237).
- Native wheel staging explicitly excludes the diagnostic executable in [build_distribution.py](D:/GIT/python/rclone-kit/scripts/build_distribution.py:200).
- Console commands construct `Rclone(...)` without embedded execution, for example [list_files.py](D:/GIT/python/rclone-kit/src/rclone_kit/cmd/list_files.py:22).
- The README still says the wheel bundles an executable and demonstrates the default constructor in [README.md](D:/GIT/python/rclone-kit/README.md:6).

Consequently, an installed wheel’s documented quick start and console commands can fail unless a system rclone happens to be on `PATH`. The task removing CLI support needs to land atomically across constructor behavior, console commands, public exports, documentation, exceptions, and wheel smoke tests.

2. The Python ownership model conflicts with the process-global Go runtime

The C ABI permits initialization exactly once per process and never permits reinitialization, even after finalization. Python, however, normally creates and owns a new `RcloneRuntime` for every embedded `Rclone`.

- The ABI’s process-global contract is explicit in [abi.h](D:/GIT/python/rclone-kit/native/rclone/librclone/rclonekit/abi.h:24).
- The Go bridge stores one global `initialized` flag in [bridge.go](D:/GIT/python/rclone-kit/native/rclone/librclone/rclonekit/bridge/bridge.go:36).
- Each embedded `Rclone` normally creates its own runtime in [client.py](D:/GIT/python/rclone-kit/src/rclone_kit/client.py:295).
- Native tests avoid the problem by sharing one session-scoped runtime in [conftest.py](D:/GIT/python/rclone-kit/tests/native/conftest.py:1).

In a real application:

- A second ordinary `Rclone(...)` instance cannot initialize.
- Closing the first client does not make another initialization possible.
- Different clients cannot select different config files.
- `find_conf_file_embedded()` consumes the one initialization slot using a throwaway runtime, making it unsafe to call before constructing the actual client; see [config_discovery.py](D:/GIT/python/rclone-kit/src/rclone_kit/config_discovery.py:96).

Before removing the CLI path, the project needs a process-wide runtime manager with reference-counted client leases and an explicit one-config-per-process policy. `Rclone.close()` should release client resources, not pretend the native runtime can be destroyed and recreated.

3. The original authorization business feature is not implemented

The rclone fork contains the required listener/redirect separation:

- `oauth_listen_addr` and `oauth_redirect_url` exist in [config.go](D:/GIT/python/rclone-kit/native/rclone/fs/config.go:570).
- The integration branch includes the OAuth commit and the C ABI bridge.

But Python currently has no authorization session API, callback relay, config-create workflow, OAuth status polling, cancellation, or result model. The existing authorization document still primarily designs a patched CLI process solution.

The missing library-based workflow needs to wrap:

- `config/providers`;
- async `config/create` or `config/update`;
- `_config.oauth_listen_addr`;
- `_config.oauth_redirect_url`;
- `config/oauthstatus`;
- `config/oauthstop`;
- callback relay routing;
- terminal job/config result extraction.

Rclone also keeps only one global active OAuth flow, as shown by `oauthCancelFn` and `oauthURL` in [oauthutil.go](D:/GIT/python/rclone-kit/native/rclone/lib/oauthutil/oauthutil.go:35). Therefore an embedded process can support only one authorization session at a time unless the fork is expanded substantially. The public API must either serialize sessions or document process isolation as the concurrency boundary.

## High-priority correctness gaps

4. Job monitoring has several lifecycle defects

The overall `JobHandle` design is good, but static inspection found concrete problems in [job.py](D:/GIT/python/rclone-kit/src/rclone_kit/job.py:86):

- `stats()` freezes after its first nonterminal snapshot. `stats_now()` fetches new statistics but retains the old cached value once one exists at [job.py](D:/GIT/python/rclone-kit/src/rclone_kit/job.py:253).
- `cancel()` says it never blocks, but synchronously calls `job/stop` and then `job/status` at [job.py](D:/GIT/python/rclone-kit/src/rclone_kit/job.py:231).
- Any transient status/parsing/RC error permanently settles and forgets the job, even though the native job may still be running, at [job.py](D:/GIT/python/rclone-kit/src/rclone_kit/job.py:312).
- Cancellation can be misclassified: any failed job observed after `cancel_requested=True` becomes cancelled, even if it independently failed during the race.
- A failed shutdown still stops the monitor thread. Retrying `Rclone.close()` cannot make progress, contrary to the exception documentation, at [job.py](D:/GIT/python/rclone-kit/src/rclone_kit/job.py:264).
- The monitor thread join has a one-second timeout whose success is not checked before runtime finalization.
- A client with an injected runtime has no closed state. Operations can be started after `Rclone.close()`, but its monitor has already been permanently stopped.

5. Native finalization is weaker than its documentation

`RcloneRuntime.close()` ignores the status and output from `RcloneKitFinalize`, then marks the wrapper closed regardless, in [runtime.py](D:/GIT/python/rclone-kit/src/rclone_kit/native/runtime.py:144).

The Go implementation calls upstream `librclone.Finalize()`, whose current body only runs garbage collection and explicitly contains a TODO about unfinished async jobs in [librclone.go](D:/GIT/python/rclone-kit/native/rclone/librclone/librclone/librclone.go:46). It does not itself stop jobs, streams, serves, mounts, or accounting.

Other resource concerns:

- Abandoned `EmbeddedFilesStream` instances are not tracked by `Rclone.close()`.
- Serve and mount handles mark themselves closed before attempting cleanup and suppress every stop error. A transient error can leak a server or mount with no possible retry; see [serve_handle.py](D:/GIT/python/rclone-kit/src/rclone_kit/serve_handle.py:49).
- Disposed serve/mount handles remain forever in the client’s tracking sets.
- All native RPC calls are serialized by one Python lock in [runtime.py](D:/GIT/python/rclone-kit/src/rclone_kit/native/runtime.py:128). A blocking list-stream pull delays job status, cancellation, and unrelated calls.

6. Linux support is not yet functionally grounded

There are Windows-shaped assumptions that the current Linux build work does not cover:

- `to_path()` treats a Unix local path such as `/tmp/data` as a remote named `/tmp/data`, producing `/tmp/data:` when reconstructed, in [util.py](D:/GIT/python/rclone-kit/src/rclone_kit/util.py:226).
- Embedded listing deliberately retains that colon-naive domain model in [listing_ops_embedded.py](D:/GIT/python/rclone-kit/src/rclone_kit/operations/listing_ops_embedded.py:59). Windows drive paths happen to reconstruct correctly; Unix local paths do not.
- `serve_http()` unconditionally splits the source on `":"`; a Unix local path raises `ValueError` before RC dispatch in [serve_ops_embedded.py](D:/GIT/python/rclone-kit/src/rclone_kit/operations/serve_ops_embedded.py:32).
- Native test discovery hardcodes `librclone_kit.dll` and `rclone.exe`, even after resolving a Linux target, in [conftest.py](D:/GIT/python/rclone-kit/tests/native/conftest.py:40).
- Every row in the parity ledger still records `linux = false`.
- CI runs unit and legacy integration tests, not `tests/native`, in [ci.yml](D:/GIT/python/rclone-kit/.github/workflows/ci.yml:78).

The Linux wheel workflow also runs directly on `ubuntu-latest`, despite the build documentation requiring the manylinux2014 container. It does not initialize the submodule or provision the pinned Go/C toolchain. The verifier checks names, hashes, ABI version, and manifests, but does not independently validate glibc symbol compatibility.

## API and functionality gaps for the library-only release

7. Public types still favor the removed CLI model

Top-level exports include `CompletedProcess`, `Process`, `Mount`, and CLI `FilesStream`, but omit several types returned by the embedded API:

- `JobHandle`;
- `MountHandle`;
- `ServeHandle`;
- `EmbeddedFilesStream`;
- `NativeBuildInfo`.

See [__init__.py](D:/GIT/python/rclone-kit/src/rclone_kit/__init__.py:5).

`copy`, `copy_to`, `purge`, and related methods still return a compatibility `CompletedProcess` whose process list, stdout, and stderr are empty under embedded execution. The class itself says it should eventually be replaced by `OperationResult` in [completed_process.py](D:/GIT/python/rclone-kit/src/rclone_kit/completed_process.py:8).

A library-only breaking release should return native domain types directly and remove process-shaped API rather than preserving misleading empty fields.

8. Some public parameters are unsupported or silently ignored

Embedded operations reject arbitrary `other_args`, which is correct during migration but becomes a permanent functionality decision once CLI fallback disappears. Examples include copy, cleanup, byte-range reads, mount, serve, listing size, and diff.

Other parameters are silently ignored:

- `verbose` across several transfer methods;
- mount `log`;
- mount `cache_dir_delete_on_exit`;
- several CLI-specific result/logging expectations.

This needs a deliberate API pass: either add typed embedded options, remove obsolete parameters in the breaking release, or raise consistently. Silent ignoring should not remain.

There is also no native logging bridge into Python, so the old `verbose`/log functionality has no real replacement for observability.

9. Python configuration state can become stale

`Rclone.config` is a snapshot created at construction. Helpers such as `is_s3()`, `get_s3_credentials()`, and `encode_fs_spec()` inspect that snapshot rather than rclone’s current loaded config.

After an embedded `config/create`, `config/update`, or future authorization flow writes credentials, rclone will know about the new remote but Python may not. This can cause:

- `listremotes()` to show a remote that `is_s3()` cannot recognize;
- S3 credential helpers to miss newly authorized configuration;
- transfer filesystem encoding to omit backend-specific settings.

This must be addressed as part of authorization: either query config through RC, refresh the Python snapshot after mutations, or stop treating `Config.text` as runtime authority.

## Validation gaps

The existing native implementation has substantial unit and local native coverage, but the release gate currently does not prove the library-only product:

- No OAuth authorization end-to-end test exists.
- No public `Rclone(...)` installed-wheel smoke test exists; wheel smoke initializes the raw runtime only in [smoke_test_installed_wheel.py](D:/GIT/python/rclone-kit/scripts/smoke_test_installed_wheel.py:89).
- No second-client/process-global-runtime test exists against the real library.
- No transient monitor failure, live progress refresh, or close-retry test exists against the real library.
- Native tests are not run in CI.
- Linux native Python tests are structurally unable to locate Linux artifacts.
- Live cloud coverage remains limited/manual.

## Recommended order

1. Make library-only packaging and API behavior internally consistent.
2. Introduce one process-wide runtime owner and define config/client lifetime semantics.
3. Fix job, stream, serve, mount, and finalization lifecycle contracts.
4. Normalize Unix/local/remote path modeling.
5. Replace process-shaped returns and exports with embedded public types.
6. Decide every formerly generic CLI option as typed, removed, or explicitly unsupported.
7. Implement the library-native authorization session and callback relay.
8. Repair CI/toolchain/submodule handling and require native Windows/Linux public-API tests before release.

The embedded operation adapters themselves are a strong foundation. The blocker is no longer RC operation coverage; it is turning a successful migration prototype into a coherent, process-safe, library-only product.