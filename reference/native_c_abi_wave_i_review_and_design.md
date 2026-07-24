# Native C ABI migration: Wave I design (public compatibility transition)

Status: partially done - C02/C06/C07/C08 (deprecation) complete; the wave's own full exit gate
("make embedded execution the default only after every non-deprecated method is complete") is
**still** not reachable - re-checked after Wave H's mount addendum resolved R01/R02, the remaining
blockers are now M04 (`config_show`) and T15 (`copy_file_s3_resumable`); see the addendum below

Date: 2026-07-24 (re-checked: 2026-07-24)

Related documents:

- [Wave H review and design](native_c_abi_wave_h_review_and_design.md)
- [CLI-to-C-ABI migration plan and ledger](rclone_cli_to_c_abi_migration_plan.md)

## 1. Scope, and why this wave cannot be fully closed yet

Ledger rows C02 (`upgrade_rclone`), C06 (`webgui`), C07 (`launch_server`), C08 (`remote_control`),
D01–D11, D19–D21. The wave's own text is explicit about its exit condition: "Make embedded execution
the default only after every non-deprecated method is complete." At the time this document was first
written, `mount()`/`mount_s3()` (Wave H, R01/R02) were the blocker - not deprecated rows, but ledger
rows still awaiting a real port, genuinely blocked on FUSE/WinFsp production build toolchain work
that did not exist yet. So flipping any default from `execution="cli"` to `execution="embedded"`, or
removing the CLI backend, was **not attempted here** - doing so would have contradicted this wave's
own stated precondition. What *is* in scope and completed here is the deprecation half: marking the
four CLI-only, never-to-be-ported methods, and adding the one new method (`native_build_info()`) the
ledger names as C02's actual replacement.

D01–D11/D19–D21 are the migration plan's own internal/distribution-removal ledger (as with D10/D11
in Wave F, D14/D15 in Wave G, D12/D13 in Wave H) - each names a gate tied to *other* rows reaching
`complete` (e.g. D02: "Embedded default has completed all public ledger rows for one compatibility
release"). At the time of writing, none of those gates were met, for the same mount-related reason
above, so none of those rows changed state in that pass either - see the addendum below for what
changed once R01/R02 landed.

## 1a. Addendum: re-checked after Wave H's mount addendum

Wave H's mount addendum (see `native_c_abi_wave_h_review_and_design.md` section 6) ported
`mount()`/`mount_s3()` (R01/R02), closing the specific blocker this document originally named. This
wave's exit gate was re-checked against the full `tests/parity/coverage.toml` ledger rather than
assumed reachable - it is **still not** reachable, but for two different, unrelated rows:

- **M04** (`config_show`): `status = "planned"`, needs a focused Go bridge extension for exact legacy
  obscure/no-obscure text behavior. This is new Go implementation work, not a build-toolchain gap,
  and was not attempted as part of the mount authorization (which covered "port `mount()`/
  `mount_s3()`, then continue Wave H/I/J from there" - a bridge feature for a different method is a
  distinct, unscoped undertaking).
- **T15** (`copy_file_s3_resumable`): `status = "planned"`. Every rclone-kit-side dependency (`access.
  copy_to`, `self.size_file`, `self.serve_http`) is now embedded-capable since Wave H's `serve_http()`
  port - re-checked directly, not left stale. The one remaining gap is unrelated to CLI-vs-embedded
  execution at all: its merge step uses a real `boto3` S3 client (`upload_part_copy`/
  `complete_multipart_upload`) regardless of execution mode, so exercising it needs live S3
  credentials this environment does not have - the same pre-existing constraint every other
  S3-multipart test in this repo already has.

Since Wave I's own exit gate requires *every* non-deprecated method complete, and M04/T15 are not,
no default was flipped and no CLI-only code was removed in this pass either. D01–D11/D19–D21 remain
unchanged for the same reason, now transitively gated on M04/T15 instead of R01/R02.

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
