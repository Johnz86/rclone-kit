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
`implementation_and_build_pipeline.md` instead.

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

## Slice 5: Pyright strict rollout - stale, re-measure before using

**The module list and error counts below predate the client-architecture
refactor (`0a9dc41`) and are now partly wrong, not just possibly drifted:**
`exec.py` no longer exists (`backend.py` and `util.py`'s `rclone_execute`
absorbed its role) and `rclone_impl.py` was renamed to `client.py`
(`RcloneImpl` no longer exists as a name - the class is `Rclone`). The two
structural findings below (cross-module `reportMissingTypeStubs` noise, and
`reportPrivateUsage` on the `detail/` extraction pattern's `self._run(...)`
calls) are still conceptually accurate - the pattern they describe still
exists, just under `Rclone._run` instead of `RcloneImpl._run` - but every
number and module name needs a fresh trial run (temporary `[tool.pyright]`
`strict = [...]` edit, reverted after measuring, per the "Working
conventions" section below) before this slice is picked up again.
`group_files.py`'s one finding from the original trial
(`reportUnnecessaryIsInstance`) is already fixed and documented in
`implementation_and_build_pipeline.md`'s typing-and-linting paragraph - drop
it from the candidate list rather than re-measuring it.

The previous revision of this file proposed re-measuring strict-mode noise
on `detail/listing_ops.py`, `detail/transfer_ops.py`, `detail/config_ops.py`,
`detail/mount_ops.py`, `detail/serve_ops.py`, `group_files.py`,
`completed_process.py`, and `exec.py` as "reasonable candidates," expecting
most to come back at or near zero. Re-running that exact trial after
slices 1-4 and the `types.py`/`chunk_store.py` work (temporary
`[tool.pyright]` `strict = [...]` edit covering those eight plus the new
`chunk_store.py`, reverted after measuring, not committed) gives a
**materially different picture than expected - read this before deciding
how to scope the next slice**:

| Module | Errors | Dominant cause |
|---|---|---|
| `chunk_store.py` | 1 | `reportMissingTypeStubs` only (see below) |
| `group_files.py` | 1 | One real finding: `reportUnnecessaryIsInstance` at line ~102 - genuine dead code, trivially fixable |
| `exec.py` | 3 | `reportMissingTypeStubs` only |
| `completed_process.py` | 4 | 1 `reportMissingTypeStubs` + 3 `reportUnnecessaryComparison` - **read the caution below before touching these** |
| `detail/mount_ops.py` | 9 | Mostly `reportMissingTypeStubs` |
| `detail/serve_ops.py` | 9 | Mostly `reportMissingTypeStubs` |
| `detail/config_ops.py` | 11 | `reportMissingTypeStubs` + 3 `reportPrivateUsage` (`_run`) |
| `detail/listing_ops.py` | 16 | `reportMissingTypeStubs` + `reportPrivateUsage` (`_run`) |
| `detail/transfer_ops.py` | 43 | **Not a good candidate despite slice 1** - still dominated by `Future[Unknown]` (missing generic type args on `ThreadPoolExecutor`/`Future` usage) and several closures with unannotated parameters (`rmdirs`, `files`, `check`, `remote`) |

97 errors total across the 9 modules, breaking down by category as:
**47 `reportMissingTypeStubs`, 19 `reportPrivateUsage`, and ~31 spread
across `reportUnknownVariableType`/`reportUnknownMemberType`/
`reportMissingParameterType`/`reportUnnecessaryComparison`/
`reportUnknownArgumentType`/`reportMissingTypeArgument`/
`reportUnnecessaryIsInstance`/`reportUnknownParameterType`.**

This surfaces two structural problems the next session needs to actually
decide on before adding any file to a real `strict = [...]` list - neither
is mechanical:

1. **`reportMissingTypeStubs` is not about missing third-party stubs
   here - it fires because a strict-mode file imports a *sibling*
   `rclone_kit` module (`util.py`, `config.py`, `rclone_impl.py`, etc.)
   that isn't itself strict-mode-clean/fully annotated** (the whole
   reason the `ANN` family is still globally ignored). In other words,
   `strict = [...]` on a single file doesn't purely measure that file's
   own quality - it also measures the annotation-completeness of
   everything it transitively imports. This means most of the "near-zero"
   candidates the previous plan expected are only near-zero *because* of
   this cross-module noise, not because the file itself has few real
   issues (`chunk_store.py` and `exec.py` are exactly this case: their
   only findings are `reportMissingTypeStubs` from importing `util.py`).
   Decide one of: (a) find or set a Pyright option that suppresses this
   specific report for first-party same-package imports without weakening
   strict mode's real value, (b) accept it as noise and count only the
   *other* categories when judging a file "clean enough" for the strict
   list, or (c) treat it as evidence that the `ANN` family needs at least
   partial progress before a broad strict rollout is worth doing.
2. **`reportPrivateUsage` (19 findings, all `RcloneImpl._run` accessed
   from `detail/*.py` sibling modules) is the `detail/` extraction
   pattern's core design working as intended** - each `detail/*.py` free
   function takes `self: RcloneImpl` and calls `self._run(...)`
   deliberately, per the public-facade phase's own established pattern.
   Python/Pyright has no "friend function" concept, so every one of these
   trips strict mode. Decide whether to suppress this per call site with a
   comment explaining the intentional pattern, or accept these files won't
   reach strict-mode-clean without a wider signature change - do not
   "fix" this by making `_run` non-private, that changes the class's
   public surface for a tooling concern.

**Caution before touching `completed_process.py`'s 3
`reportUnnecessaryComparison` findings** (lines ~30, ~39, ~47 - the
`stdout is not None`/`stderr is not None`/`rtn is None` guards): typeshed
types `subprocess.CompletedProcess[str]`'s `.stdout`/`.stderr` fields as
plain `str`, not `str | None`, even though at runtime they genuinely are
`None` whenever a command ran without capturing output (`rclone_execute`'s
`capture=False` path does exactly this). Pyright strict mode's complaint
here is a **false positive relative to actual runtime behavior**, not
dead code - removing the `stdout`/`stderr` guards would introduce a real
`None`-handling bug the type checker just can't see, given typeshed's
imprecision. The `rtn is None` check on `returncode` is a different case
and worth its own look - `Popen.communicate()` always populates
`returncode` by the time a `CompletedProcess` is built, so that one might
actually be legitimately dead now; verify with a quick trace before
touching it, don't assume all three are the same shape just because
Pyright reports them identically.

## Everything else in the roadmap (lower priority, do after this or independently)

From `docs/implementation_and_build_pipeline.md`'s roadmap table:

- **Release publication** - CI assembles verified wheels but doesn't
  publish/attest them; needs PyPI trusted publishing, an
  approval-protected environment, a tag-driven publish job, and artifact
  attestations. Not started.
- **Build isolation** - smoke tests poison proxies but don't enforce
  network denial; run them in a network-disabled container/namespace
  where supported. Not started.
- **Source distributions** - no sdist-to-wheel build path exists yet;
  either keep wheel-only releases deliberately, or add a verified-artifact
  input/download hook and test sdist builds on every target. Not started,
  and may be a deliberate non-goal - confirm intent before investing here.

Two open decisions survive from the now-removed
`docs/rclone_architecture_refactor_plan.md`, not part of the formal roadmap
table:

- **Rename `detail/` to `operations/`.** Deferred deliberately as its own
  mechanical, low-risk rename - do it as a single commit with no other
  changes mixed in, since it touches every operation module's import path.
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
