# Native C ABI migration: Wave J status (executable-free wheels)

Status: blocked - not started, correctly, not silently. Re-checked after Wave H's mount addendum
resolved R01/R02, M04 (`config_show`) was completed, and T15 (`copy_file_s3_resumable`) was upgraded
to `native_tested` (the most this environment can verify without live S3 credentials): every
implementation gap this wave's precondition chain names is now closed or explicitly non-blocking.
What remains is a release-process/policy decision, not an implementation gap - see the addendum below

Date: 2026-07-24 (re-checked: 2026-07-24; M04 completed: 2026-07-24; T15 upgraded: 2026-07-24)

## Why Wave J cannot begin yet

Ledger rows D02, D06, D07, D16, D17, D18 - Wave J's entire scope - are every one of them gated on
CLI compatibility mode already being removed from the supported release:

- D02: "Embedded default has completed all public ledger rows for one compatibility release."
- D06: "CLI compatibility mode removed from the supported release."
- D07: "Install command and docs removed."
- D16: "Transitional dual-artifact release ends."
- D17: "Wheels and resolver require only DLL/SO."
- D18: "C-ABI-only release verification rejects executable assets."

None of these preconditions hold:

1. `execution="cli"` is still this library's **default** execution mode. It is no longer the only
   way to reach `mount()`/`mount_s3()` - Wave H's mount addendum ported both (R01/R02) to embedded
   execution via a new `--profile production` build (FUSE/WinFsp toolchain wiring) - M04
   (`config_show`) has since been completed too, and T15 (`copy_file_s3_resumable`) is now
   `native_tested` rather than `planned`. No row in `tests/parity/coverage.toml` still represents an
   open implementation gap - the default has simply not been flipped yet, which is Wave I's own call
   to make, not something this document decides.
2. Wave I's own exit gate ("make embedded execution the default only after every non-deprecated
   method is complete") is consequently still not met (`native_c_abi_wave_i_review_and_design.md`'s
   addendum).
3. "Publish one compatibility release retaining explicit CLI mode and collect failures before final
   removal" (Wave I's own text) has not happened - that is a release-process step, not a code change,
   and it comes *after* point 2, not before it.

Wave J's own scope statement is explicit that it is destructive: "Remove executable staging, runtime
download, resolver, hashes, and wheel assets." Removing `util.get_rclone_exe`/the executable
resolver/wheel executable assets now would break every `execution="cli"` deployment - which is still
the default a caller gets by simply constructing `Rclone(...)` without `execution="embedded"` - for
no compensating benefit until that default actually changes. Doing so would contradict every one of
D02/D06/D07/D16-D18's own stated removal gates, not merely skip ahead of them.

## Addendum: what actually changed, and what still needs to happen

The FUSE/WinFsp production-toolchain undertaking this document originally listed as step 1 is done:
`scripts/native/build.py --profile production` exists, and R01/R02 are ported and tested (Windows;
Linux FUSE was not attempted - out of scope for the authorization that covered this work, which named
WinFsp/Windows specifically). M04 (`config_show`) - originally step 1 in this list - is also done
(a new `rclonekit/configshow` Go RC method; see `native_c_abi_wave_i_review_and_design.md`'s M04
addendum). T15 (`copy_file_s3_resumable`) - the row this document previously named as the last
implementation blocker - is now `native_tested`: every rclone-kit-side code path (`size_file`,
`serve_http`, ranged download, `copy_to`) is proven against the real native library using a local
directory in place of an S3 bucket, since none of that machinery is actually S3-specific. Only the
S3-only merge step (a real `boto3` call) remains unverified by automation, and a research pass
confirmed this repo's own end-to-end test for it was *already* an unconditional manual-only skip
before this migration touched anything - not a gap this migration introduced or can close. What
remains, in order:

1. Embedded execution made the default (Wave I's own exit gate) - see
   `native_c_abi_wave_i_review_and_design.md`'s third addendum for why this is now a policy question
   rather than an implementation one, raised back to the user rather than decided here.
2. A compatibility release published with CLI mode still explicitly available, and a period to
   collect failures from it (Wave I's own text) - a release/support-cycle step, not a code change.
3. Only then does Wave J's actual removal work - deleting the executable resolver, downloader,
   wheel assets, and hashes - become safe to do without breaking the still-default, still-only-fully-
   working execution mode.

This document intentionally contains no design decisions, because none could be made responsibly:
there is nothing to design until the precondition chain above is satisfied. It exists so a future
reader (human or otherwise) does not mistake Wave J's absence for an oversight.
