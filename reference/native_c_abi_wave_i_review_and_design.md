# Native C ABI migration: Wave I design (public compatibility transition)

Status: partially done - C02/C06/C07/C08 (deprecation) complete; the wave's own full exit gate
("make embedded execution the default only after every non-deprecated method is complete") is not
yet reachable, since Wave H's R01/R02 (`mount`/`mount_s3`) remain blocked on a production build
toolchain gap

Date: 2026-07-24

Related documents:

- [Wave H review and design](native_c_abi_wave_h_review_and_design.md)
- [CLI-to-C-ABI migration plan and ledger](rclone_cli_to_c_abi_migration_plan.md)

## 1. Scope, and why this wave cannot be fully closed yet

Ledger rows C02 (`upgrade_rclone`), C06 (`webgui`), C07 (`launch_server`), C08 (`remote_control`),
D01–D11, D19–D21. The wave's own text is explicit about its exit condition: "Make embedded execution
the default only after every non-deprecated method is complete." `mount()`/`mount_s3()` (Wave H,
R01/R02) are not deprecated rows - they are ledger rows still awaiting a real port - and they remain
genuinely blocked on FUSE/WinFsp production build toolchain work that does not exist yet (confirmed,
not assumed, in Wave H's own design doc). So flipping any default from `execution="cli"` to
`execution="embedded"`, or removing the CLI backend, is **not attempted here** - doing so would
contradict this wave's own stated precondition. What *is* in scope and completed here is the
deprecation half: marking the four CLI-only, never-to-be-ported methods, and adding the one new
method (`native_build_info()`) the ledger names as C02's actual replacement.

D01–D11/D19–D21 are the migration plan's own internal/distribution-removal ledger (as with D10/D11
in Wave F, D14/D15 in Wave G, D12/D13 in Wave H) - each names a gate tied to *other* rows reaching
`complete` (e.g. D02: "Embedded default has completed all public ledger rows for one compatibility
release"). None of those gates are met yet, for the same mount-related reason above, so none of
those rows change state in this pass either - they are reviewed, not silently left stale.

## 2. Design decisions

### I1 - Deprecate in place; do not change behavior

C06/C07/C08 (`webgui`/`launch_server`/`remote_control`) are CLI-only utility methods with no planned
embedded port (per their own ledger notes: web GUI management isn't a storage-library concern;
`execution="embedded"` already provides direct in-process RC with no external `rclone rcd` needed;
`remote_control()`'s "drive a separate, externally-addressed rclone process" meaning is unrelated to
this client's own execution mode). Each now emits `DeprecationWarning` via `warnings.warn(...,
stacklevel=2)` on every call, with a message naming the specific reason and (where one exists) the
replacement - but their actual behavior is completely unchanged. This is intentionally the smallest
possible change: a caller relying on any of these three today keeps working identically, with a
warning surfaced through Python's normal deprecation-warning channel (visible under `-W`, pytest's
own warning capture, etc.), not a behavior change to react to.

### I2 - C02 (`upgrade_rclone`) needed a real replacement, not just a warning

Unlike C06-C08, C02's own ledger note specifies an actual replacement: "packaged-native validation
and `native_build_info()`." `Rclone.native_build_info()` is new: it wraps the already-existing
`RcloneRuntime.build_info()` (ABI version, rclone version/commit, Go version, build tags, target
platform - all already implemented since the C-ABI bridge's first slice, just never exposed on
`Rclone` itself). It is embedded-only (raises `EmbeddedOnlyOperationError` under `execution="cli"`):
the native library is linked at build/package time under embedded execution, never downloaded or
verified per call the way `upgrade_rclone()`'s CLI-mode executable-download flow is, so there is no
CLI-mode equivalent question to answer. `upgrade_rclone()` itself also now warns, pointing at this
new method as what an embedded deployment should query instead.

## 3. Test coverage

Unit tests (`tests/unit/test_deprecated_methods.py`) assert each of the four methods still performs
its original action while emitting exactly the expected `DeprecationWarning`, and that
`native_build_info()` raises `EmbeddedOnlyOperationError` under a bare (non-embedded) client. Native
tests confirm `native_build_info()` reports real values (a positive ABI version, non-empty rclone/Go
version strings) against the built DLL, and that the CLI-backed client still lacks it entirely.
