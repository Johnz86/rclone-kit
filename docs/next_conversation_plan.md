# Next-conversation plan: typing and linting

**How to use this file:** paste a pointer to this file (or its content) as
the opening prompt for the next `rclone-kit` work session. It is written to
be self-contained - a fresh session with no memory of prior conversations
should be able to pick this up and execute it directly. It assumes the
working directory is the repo root and that `uv sync --locked --all-groups
--all-extras` has already been run.

## Where things stand

Every roadmap phase through the client-architecture refactor and test
isolation is complete and committed on `main` (see
`docs/implementation_and_build_pipeline.md`'s "Improvement roadmap" section
for the full history). A follow-up review of the client-architecture commit
(`0a9dc41`) against `docs/rclone_architecture_refactor_plan.md` and
`docs/code_style.md` found the import boundaries hold and no behavioral
bugs, and fixed the small deviations it did find - see the
"client-architecture phase" paragraphs in `implementation_and_build_pipeline.md`
for the full account. `docs/rclone_architecture_refactor_plan.md` itself was
removed afterward: it described the pre-refactor architecture as current,
and every phase it planned is now complete and narrated in
`implementation_and_build_pipeline.md` instead. `detail/` was then renamed
to `operations/` as its own mechanical commit, and the Pyright strict
rollout below started with its first three modules.

The typing-and-linting phase's first pass is also done: slices 1-4 from
this file's previous revision all landed -

1. `subprocess.CompletedProcess` parameterized as `CompletedProcess[str]`
   everywhere it flows (`util.py`, `exec.py`, `rclone_impl.py`,
   `completed_process.py`, `detail/transfer_ops.py`).
2. `S324` resolved (`hashlib.md5(usedforsecurity=False)`) and removed from
   the ignore list.
3. `PLR2004` in `src/` resolved (8 named constants) and the ignore
   narrowed to a `tests/**/*.py`-scoped `per-file-ignores` entry instead of
   a blanket global one.
4. `ARG002` resolved (5 findings, `del`-based suppression matching the
   existing `fetch_config_paths` pattern) and removed from the ignore
   list entirely.

`pyproject.toml`'s `[tool.ruff.lint]` `ignore` list is now down to 18
entries (from 20).

Two things happened alongside that plan, worth knowing about even though
neither was originally on it:

- **A `types.py` refactor and `chunk_store.py` extraction.** The `PLR2004`
  slice above only named the one magic value Ruff flagged and left every
  other `1024**N` in the same function as a bare literal; asked to redo it
  properly, the fix replaced the whole encode/decode ladder with one
  `_SIZE_SUFFIX_UNITS` table, folded `SizeSuffix`'s and `PartInfo`'s
  private single-use helper functions into methods on the type that owns
  them, and extracted the unrelated `get_chunk_tmpdir`/`_clean_old_files`
  singleton (never actually a "type") into a new `chunk_store.py`. This is
  why `chunk_store.py` now exists as a strict-mode candidate below.
- **A lazy atexit-registration fix**, unrelated to linting/typing but
  surfaced by the same "does this deviate from established pattern"
  scrutiny: `util.py`, `process.py`, and `file_part.py` used to register
  their `atexit` cleanup handlers as a side effect of merely
  `import rclone_kit`-ing the package, regardless of whether the
  corresponding resource (a temp config file, a live subprocess, a
  long-lived `Process`, a staged chunk file) was ever created. Fixed by
  moving each registration to the first actual creation of the resource
  it protects, guarded by a lock-plus-flag idiom matching
  `chunk_store.get_chunk_tmpdir`'s own first-use guard - confirmed
  empirically (`import rclone_kit` in a fresh interpreter now registers
  zero of this library's `atexit` handlers, down from four) and covered by
  `tests/unit/test_atexit_lazy_registration.py`. `mount_util.py` and
  `upload_parts_resumable.py` needed no change; they were already lazy for
  an unrelated reason (both are only ever imported function-locally at
  their one real call site). This is fully done and committed. The one
  cosmetic follow-up this note used to list - their
  `_register_exit_cleanup_handlers()` docstrings reading "registered once,
  at import time" inconsistently next to the other three modules' "first
  time X is created" phrasing - is also done: both now explain why "at
  import time" is still correct for them specifically.

## Before touching any code: re-run the safety net

```bash
uv run ruff format . && uv run ruff check . && uv run pyright src tests scripts && uv run pytest tests/unit tests/integration -q
```

Confirm it's green before starting.

## Current ignore-list state (re-verify before starting - counts will have
shifted since this was written)

Read the rationale comment block directly above `[tool.ruff.lint]`'s
`ignore = [...]` in `pyproject.toml` in full before touching anything -
several entries are documented as deliberately permanent, not a to-do.

**Permanent - do not spend a slice trying to "shrink" these:** `PLC0415`,
`S603`, `E501`, `T201` (unchanged reasoning from before, see the comment
block).

**Large, genuinely deferred work - each needs its own multi-slice pass in
a future session** (counts re-measured, not carried over from the last
revision of this file):

| Family | Count | Notes |
|---|---|---|
| `S101` (bare `assert`) | 521 | Same per-call-site judgment as before: should-never-happen invariant (keep) vs. real error condition (raise). |
| `ANN` (missing annotations) | 202 | Unchanged in shape from before. |
| `TRY` (exception-handling style) | 144 | Unchanged in shape from before. |
| `FBT001`/`FBT002`/`FBT003` | 143+82+17 = 242 | Breaking-API-change category, needs a deprecation path. |
| `A001`/`A002` (builtin shadowing) | 16+7 = 23 | Same breaking-change category as `FBT`. |
| `PLR0913` (too many arguments) | 33 | Per-site decomposition judgment. |
| `PTH` (`os.path` -> `pathlib`) | 33 | Broad, spans many files. |
| `PLR0911`/`PLR0912`/`PLR0915` | 1+3+6 = 10 | Real refactoring, not mechanical. |

Counts re-measured during the client-architecture follow-up review
(2026-07-20); re-run `uv run ruff check --select <CODE> --no-cache .` before
trusting any of these in a later session - they will have moved again.

## Slice 5: Pyright strict rollout - three modules landed, re-trialed with fresh data

`[tool.pyright]`'s `strict = [...]` now permanently covers `command_flags.py`,
`settings.py`, and `group_files.py` - all three came back genuinely 0 errors
in a fresh trial run against the post-`operations/`-rename tree, and the
reason turns out to be simple rather than a sign of exceptional annotation
quality: all three have **zero `rclone_kit`-internal imports** (stdlib only -
`dataclasses`, `pathlib`, `os`, `warnings`). That's a real, permanent, valid
win (nothing to revert), but it also means these three don't yet prove the
cross-module noise problem below is solved for a module that actually
imports siblings.

A fresh trial (temporary `strict = [...]` edit covering the six best
remaining candidates, reverted after measuring, not committed) gives:

| Module | Errors | Dominant cause |
|---|---|---|
| `chunk_store.py` | 1 | `reportMissingTypeStubs` only, from importing `util.py` |
| `completed_process.py` | 3 | 1 `reportMissingTypeStubs` + 2 `reportUnnecessaryComparison` (the `stdout`/`stderr is not None` guards - **read the caution below, these are false positives, do not remove them**) |
| `config_discovery.py` | 2 | `reportMissingTypeStubs`, from importing `runtime/exceptions.py` and `util.py` |
| `backend.py` | 3 | `reportMissingTypeStubs`, from importing `config.py`/`process.py`/`util.py` |
| `access.py` | 5 | `reportMissingTypeStubs`, from importing `dir.py`/`dir_listing.py`/`file.py`/`remote.py`/`types.py` |

A third finding used to be in this table - `completed_process.py`'s
`rtn is None` check on `returncode` - and is now fixed and removed from the
property entirely (`Popen.communicate()` always populates `returncode` with
a real `int`, confirmed by tracing both construction sites in `util.py` and
`operations/transfer_ops.py`; no caller ever checked for `None`).

This still surfaces the one structural problem that matters for scoping the
next slice: **every remaining candidate's only finding is
`reportMissingTypeStubs` from importing a non-strict sibling module** - not
about missing third-party stubs, but Pyright strict mode measuring the
annotation-completeness of everything a file transitively imports, not just
the file itself (the reason the `ANN` family is still globally ignored).
Decide one of: (a) find or set a Pyright option that suppresses this
specific report for first-party same-package imports without weakening
strict mode's real value, (b) accept it as noise and count only the *other*
categories when judging a file "clean enough" for the strict list, or
(c) treat it as evidence that the `ANN` family needs at least partial
progress before broadening the strict list further - this is the
provisional choice reflected in `implementation_and_build_pipeline.md`'s
roadmap table right now, not a final decision.

The `detail/` extraction pattern's `reportPrivateUsage` finding
(`Rclone._run` accessed from operation-module sibling functions, the same
`RcloneImpl._run` finding from the pre-rename trial under its new name) was
not re-measured this pass since none of the five candidates above are
`operations/*.py` files - re-trial those specifically before relying on this
note; the design tradeoff is unchanged: Python/Pyright has no "friend
function" concept, so this trips strict mode by design, not by mistake - do
not "fix" it by making `_run` non-private, that changes `Rclone`'s public
surface for a tooling concern.

**Caution before touching `completed_process.py`'s 2 remaining
`reportUnnecessaryComparison` findings** (the `stdout is not None`/
`stderr is not None` guards): typeshed types
`subprocess.CompletedProcess[str]`'s `.stdout`/`.stderr` fields as plain
`str`, not `str | None`, even though at runtime they genuinely are `None`
whenever a command ran without capturing output (`rclone_execute`'s
`capture=False` path does exactly this). Pyright strict mode's complaint
here is a **false positive relative to actual runtime behavior**, not dead
code - removing these guards would introduce a real `None`-handling bug the
type checker just can't see, given typeshed's imprecision.

## Everything else in the roadmap (lower priority, do after this or independently)

From `docs/implementation_and_build_pipeline.md`'s roadmap table:

- **Release publication** - done: `.github/workflows/release.yaml` builds,
  verifies, and publishes both certified wheels to PyPI via trusted
  publishing on `v*` tags, gated by the `pypi-release` GitHub Environment.
  Artifact attestations (`actions/attest-build-provenance` or equivalent)
  are not added yet - optional hardening, not required for the pipeline to
  work.
- **Build isolation** - smoke tests poison proxies but don't enforce
  network denial; run them in a network-disabled container/namespace
  where supported. Not started.
- **Source distributions** - no sdist-to-wheel build path exists yet;
  either keep wheel-only releases deliberately, or add a verified-artifact
  input/download hook and test sdist builds on every target. Not started,
  and may be a deliberate non-goal - confirm intent before investing here.

One open decision survives from the now-removed
`docs/rclone_architecture_refactor_plan.md`, not part of the formal roadmap
table (`detail/` → `operations/` is done, see
`implementation_and_build_pipeline.md`'s client-architecture paragraphs):

- **Decide whether `RcloneBackend` stays a private extension boundary or
  becomes a documented public extension point.** Currently private (the
  safe default per `implementation_and_build_pipeline.md`); revisit only if
  a real caller needs a custom backend.

One loose end surfaced while investigating the `scan_missing_folders` bug,
not part of the formal roadmap table, and deliberately **not** attempted
this session:

- `tests/cloud/test_scan_missing_folders.py`'s only test compares `src` to
  itself (`src="dst:rclone-kit-unit-test", dst="dst:rclone-kit-unit-test"`),
  so it can't exercise a real diff even when un-skipped. It should use two
  distinct paths under the same bucket - but picking those paths requires
  knowing the live bucket's actual layout (the only other cloud test
  referencing this bucket points at one specific known object,
  `zachs_video/breaking_ai_mind.mp4`, implying fixed real content rather
  than an empty scratch bucket). This is a `@unittest.skip`'d,
  credential-gated manual test with no way to run or verify a fix without
  live bucket access, so guessing at replacement paths risks producing a
  test that still silently validates nothing - worse than the honestly-labeled
  gap it has now. Whoever owns live-bucket access for this project should
  pick the two paths and verify the rewritten test actually round-trips a
  real diff before relying on it.
- Check whether anything outside this repo parses the `"created"` field
  `s3/multipart/info_json.py` persists (grep of this repo found nothing -
  it's write-only here) before ever changing `datetime.now()` to
  `datetime.now(tz=timezone.utc)` for `DTZ005` - the serialized ISO format
  changes (gains a `+00:00` suffix), which is exactly why the ignore-list
  comment already flags this one as "out of scope for a lint-tooling
  migration." Not included as a slice above because the risk call belongs
  to whoever owns the resumable-upload feature, not a typing/linting pass.

## Working conventions to follow (established over the prior phases)

- One slice = one commit. Run the full verification gate before every
  commit (command above).
- `git status`/`git diff` before every `git add`; stage only the specific
  files you touched (`git add -- path1 path2`), never `git add -A`/`.` -
  there have been unrelated, in-progress changes from the repo owner in
  the working tree throughout this project's history (e.g.
  `.github/workflows/ci.yml`, `scripts/build_distribution.py`) that must
  never be swept up into an unrelated commit.
- Commit author must be `Jan Jakubcik <jakubcikjan@gmail.com>`, no
  `Co-Authored-By` trailer, imperative subject line under ~70 chars, body
  explains WHY not WHAT (see `docs/code_style.md` and recent `git log` for
  the established tone).
- No inline comments unless the WHY is genuinely non-obvious; no magic
  strings/numbers; KISS/DRY; decompose into functions with docstrings
  instead of comment blocks. When naming a magic value, name *every*
  occurrence of that same meaning in the function, not just the one the
  linter happened to flag (this exact gap is why the `types.py` refactor
  above was needed as a follow-up to slice 3).
- Before believing any finding count in this document, re-run the
  `uv run ruff check --select <CODE> --no-cache .` command that produced
  it - the codebase will have moved since this was written.
- When trialling Pyright strict mode, use a temporary `[tool.pyright]`
  edit and revert it before committing anything unrelated - don't leave
  experimental config in a slice's diff. Categorize what comes back by
  *report type*, not just a raw count - as this revision's trial shows, a
  low error count can still hide a structural issue (cross-module
  `reportMissingTypeStubs` noise) rather than reflect the file's own
  quality.
- After finishing any slice(s) here, update
  `docs/implementation_and_build_pipeline.md`'s roadmap table and prose
  the same way every prior phase did: mark the row done (or narrow its
  remaining scope) and add a paragraph summarizing what changed and why,
  same as the prior phases already documented there.
