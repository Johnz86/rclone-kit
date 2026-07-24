# Native C ABI migration: Wave I design (public compatibility transition)

Status: partially done - C02/C06/C07/C08 (deprecation) complete; M04 (`config_show`) also now
complete (second addendum below); T15 (`copy_file_s3_resumable`) upgraded from `planned` to
`native_tested` (third addendum below) - every rclone-kit-side code path is implemented and verified
against the real native library, and this is now the maximum this environment can verify without
live S3 credentials. Every other row in `tests/parity/coverage.toml` is either at a test-backed
status or explicitly non-blocking (C09) or correctly gated on Wave J's own removal precondition chain
(C04/C05). Whether to flip `execution="embedded"` to the default now, given this state, is a policy
call flagged back to the user rather than made unilaterally - see the third addendum.

Date: 2026-07-24 (re-checked: 2026-07-24; M04 addendum: 2026-07-24; T15 addendum: 2026-07-24)

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

## 1b. Addendum: M04 (`config_show`) completed

`config show`/`config show <remote>` had no RC equivalent at all: `config/dump` and `config/get`
exist but return JSON, not this command's plain-text (INI-format) output. Added a new Go RC method,
`rclonekit/configshow` (`native/rclone/librclone/rclonekit/rc/configshow.go`), that reproduces
`config.ShowConfig()`/`config.ShowRemote()`'s exact text byte-for-byte - verified directly against
the real CLI executable with a temporary config file containing a password field, not merely
assumed from reading the Go source: same `\r\n`-terminated whole-file serialization, same
`\n`-terminated single-remote section, same `*** ENCRYPTED ***` masking for password-typed options
with a non-empty value, same `# couldn't find type of fs for "name"` comment for an unknown remote.

Investigating this uncovered a genuine, pre-existing bug unrelated to this migration: `fetch_config_
show()`'s CLI implementation passes `--obscure`/`--no-obscure` to `rclone config show`, but that
subcommand has no such flags at all - only `config create`/`config update` do (confirmed by reading
`cmd/config/config.go`, where those flags are registered exclusively on `configCreateCommand`/
`configUpdateCommand`'s own `FlagSet`s). Calling `Rclone.config_show(obscure=True)` under
`execution="cli"` today crashes with `unknown flag: --obscure` against a real build - confirmed
empirically, not merely inferred - which the CLI unit test never caught since it only exercises a
faked subprocess backend. Fixing that latent CLI bug is out of scope for this migration (whose
premise is behavioral parity, not new features); `fetch_config_show_embedded()` instead raises
`UnsupportedEmbeddedOperationError` for either flag, an honest "not supported" rather than silently
reproducing the CLI's crash or pretending either flag has some effect it does not.

`config show`'s own display logic (`ShowRemote`) never actually decrypts anything either, despite
its own `Short` help text claiming "(decrypted) config file" - password-typed fields always show as
the literal string `*** ENCRYPTED ***` when set, everything else shows its raw stored value
verbatim. There is no controllable "obscure vs. not" toggle in this command's actual behavior at
all; the challenge this row's ledger note called out was getting that always-on masking text exactly
right in a from-scratch Go reimplementation, not honoring a flag that turns out not to exist.

## 1c. Addendum: T15 (`copy_file_s3_resumable`) upgraded to `native_tested`, and a policy question

T15 was `planned` with the note "every rclone-kit-side dependency is now embedded-capable... needs
live S3 credentials this environment does not have." Before accepting that as a final answer, a
research pass confirmed there was more real, honest progress available without any credentials:
`upload_parts_resumable()` calls `self.size_file(src)` and `self.serve_http(src_dir)` directly on its
`access` parameter, then downloads byte ranges from that HTTP server and re-uploads each chunk via
`access.copy_to(chunk, dst_part)` - none of that is S3-specific. Pointing `dst_dir` at a plain local
directory instead of a bucket exercises the *exact same* code path with no code change at all. A new
native test (`tests/native/test_upload_parts_resumable_embedded_integration.py`) now runs
`upload_parts_resumable()`/`copy_file_parts_resumable()` for real against the embedded client and a
real `serve_http()` server - part splitting, ranged downloads, reassembly, and the resumable
skip-already-uploaded-parts behavior are all verified against the real native library. Only
`upload_parts_server_side_merge.py`'s call (the one genuinely S3-only piece, using `boto3`'s
`upload_part_copy`/`complete_multipart_upload` against a real bucket) is faked out.

A background research pass also confirmed this is not a gap this migration introduced: this repo's
own `tests/cloud/test_copy_file_resumable_s3.py::test_copy_parts` - the actual end-to-end test for
this exact functionality - is `@unittest.skip("Manual test...")`, unconditionally, regardless of
whether credentials are present. It was never part of the automated suite, before or after this
migration. T15's status moved from `planned` to `native_tested` to reflect this real, verified
progress; it cannot honestly move further (to `cli_parity_tested`) without live credentials and
deliberately un-skipping that manual test - genuinely outside what this environment or further
coding can do.

**The policy question this leaves**: Wave I's own exit gate text - "make embedded execution the
default only after every non-deprecated method is complete" - read literally against the ledger's own
terminal `complete` status, is circular (no row can reach the ledger's final `complete` step, which
includes "embedded default enabled," until the default is already flipped). Read as intended - every
row's actual implementation is done and tested as far as this environment allows - that condition now
holds: every remaining `planned` row is either T15 (verified to the fullest extent achievable without
external credentials), C09 (`get_verbose`, explicitly annotated "not a migration blocker either way"),
or C04/C05 (`remove` decision rows correctly gated on Wave J's own removal precondition chain, not on
any open implementation gap). Flipping `execution="embedded"` to the default is nonetheless a
significant, hard-to-reverse-in-spirit behavior change affecting every existing caller of this
library - this document does not make that call unilaterally; it is raised back to the user as a
decision only they can make, alongside this session's other work.

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

M04 (`config_show`): unit tests (fake `RcClient`) cover the whole-config and single-remote request
shapes and the `obscure`/`no_obscure` rejection paths. A native test asserts CLI/embedded parity for
an unconfigured remote name (safe regardless of what either backend's own config file actually
contains - see the test module's docstring) plus a whole-config sanity check and both rejection
paths against the real built library.

T15 (`copy_file_s3_resumable`): native tests
(`tests/native/test_upload_parts_resumable_embedded_integration.py`) run the full non-merge upload
path for real against the embedded client and a real `serve_http()` server, using a local directory
in place of an S3 bucket - part splitting/range-download/reassembly, resumable skip-already-done
behavior, and `copy_file_parts_resumable()`'s own orchestration with only the S3-only merge call
faked out. No unit test was added on top of this: the orchestration-level behavior
(`test_copy_file_parts_resumable.py`) and the merge logic in isolation
(`test_upload_parts_server_side_merge.py`) already had unit coverage before this addendum.
