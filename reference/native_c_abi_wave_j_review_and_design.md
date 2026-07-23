# Native C ABI migration: Wave J status (executable-free wheels)

Status: blocked - not started, correctly, not silently

Date: 2026-07-24

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

1. `execution="cli"` is still this library's **default** execution mode, and remains the only way
   to reach `mount()`/`mount_s3()` (Wave H, R01/R02) - genuinely blocked on FUSE (Linux)/WinFsp
   (Windows) production build toolchain work that does not exist yet (confirmed empirically in
   `native_c_abi_wave_h_review_and_design.md`: `mount/mount` is wired and fails honestly, but no
   platform mount implementation is compiled in).
2. Wave I's own exit gate ("make embedded execution the default only after every non-deprecated
   method is complete") is consequently not met either (`native_c_abi_wave_i_review_and_design.md`).
3. "Publish one compatibility release retaining explicit CLI mode and collect failures before final
   removal" (Wave I's own text) has not happened - that is a release-process step, not a code change,
   and it comes *after* point 2, not before it.

Wave J's own scope statement is explicit that it is destructive: "Remove executable staging, runtime
download, resolver, hashes, and wheel assets." Removing `util.get_rclone_exe`/the executable
resolver/wheel executable assets now would break every `execution="cli"` deployment - which is still
the default a caller gets by simply constructing `Rclone(...)` without `execution="embedded"` - for
no compensating benefit, since embedded execution cannot yet reach full parity (mount is missing).
Doing so would contradict every one of D02/D06/D07/D16-D18's own stated removal gates, not merely
skip ahead of them.

## What would need to happen first, in order

1. A production build profile with FUSE (Linux) and WinFsp (Windows) toolchain wiring, so
   `mount()`/`mount_s3()` (R01/R02) can actually be ported - a distinct, substantial infrastructure
   undertaking (new build tooling, driver headers/linking, privileged-mount test harness) from any
   of the RC-wiring work Waves D-I did.
2. R01/R02 themselves ported and tested on supported OS runners (closing D12's gate too).
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
