# Native C ABI migration: Wave J status (executable-free wheels)

Status: blocked - not started, correctly, not silently. Re-checked after Wave H's mount addendum
resolved R01/R02: the FUSE/WinFsp toolchain gate that originally headed this precondition chain is
gone, but the chain is still not satisfied (see the addendum below)

Date: 2026-07-24 (re-checked: 2026-07-24)

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
   execution via a new `--profile production` build (FUSE/WinFsp toolchain wiring) - but two
   unrelated ledger rows, M04 (`config_show`) and T15 (`copy_file_s3_resumable`), are still
   `planned`, so "every non-deprecated method is complete" still does not hold.
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
no compensating benefit, since embedded execution cannot yet reach full parity (M04/T15 remain).
Doing so would contradict every one of D02/D06/D07/D16-D18's own stated removal gates, not merely
skip ahead of them.

## Addendum: what actually changed, and what still needs to happen

The FUSE/WinFsp production-toolchain undertaking this document originally listed as step 1 is done:
`scripts/native/build.py --profile production` exists, and R01/R02 are ported and tested (Windows;
Linux FUSE was not attempted - out of scope for the authorization that covered this work, which named
WinFsp/Windows specifically). What remains, in order:

1. M04 (`config_show`) - a focused Go bridge extension for exact legacy obscure/no-obscure text
   behavior. New Go implementation work, distinct from anything mount-related.
2. T15 (`copy_file_s3_resumable`) - needs live S3 credentials to actually exercise its merge step
   (a real `boto3` client call, unrelated to CLI-vs-embedded execution); this environment does not
   have them, the same constraint every other S3-multipart test here already has.
3. Every other still-open ledger row (Wave I's remaining D0x/D1x/D2x rows, whatever else has not
   reached `complete`) actually complete.
4. Embedded execution made the default (Wave I's own exit gate, only then reachable).
5. A compatibility release published with CLI mode still explicitly available, and a period to
   collect failures from it (Wave I's own text) - a release/support-cycle step, not a code change.
6. Only then does Wave J's actual removal work - deleting the executable resolver, downloader,
   wheel assets, and hashes - become safe to do without breaking the still-default, still-only-fully-
   working execution mode.

This document intentionally contains no design decisions, because none could be made responsibly:
there is nothing to design until the precondition chain above is satisfied. It exists so a future
reader (human or otherwise) does not mistake Wave J's absence for an oversight.
