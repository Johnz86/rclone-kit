# Release process

This document covers the exact release command sequence, what a release
record must capture, and how PyPI publishing is authorized without a stored
long-lived token.

## Release command sequence

Run from a clean checkout of the commit being released, on each certified
target platform (currently Windows AMD64 and Linux AMD64 — see
`src/rclone_kit/runtime/native_platform.py`'s `SUPPORTED_NATIVE_TARGETS`):

```powershell
uv sync --locked --all-groups --all-extras
uv run ruff format --check .
uv run ruff check .
uv run pyright _build_backend.py src tests scripts
uv run pytest tests/unit
uv run python scripts/native/verify_submodule_pin.py
uv run python scripts/native/build.py --target <windows|linux>-amd64 --profile production
uv run pytest tests/native
uv run python scripts/build_distribution.py --target <windows|linux>-amd64 --out-dir dist
uv publish --check-url https://pypi.org/simple
```

This is the same order `.github/workflows/release.yaml` runs: quality gates,
then the submodule pin check, then the native build, then `tests/native`
against that freshly built library, then the wheel.

Two details matter when actually executing it:

- **One canonical command replaces the manual staging/build/verify/smoke-test
  sequence.** `scripts/build_distribution.py` (see
  `docs/implementation_and_build_pipeline.md`) builds the certified native
  library from the `native/rclone` submodule with `--profile production`,
  stages it into an isolated temporary copy of the source tree, verifies the
  staged library's SHA-256 against its shipped manifest, builds exactly one
  wheel, runs every `scripts/verify_distribution.py` check, installs the
  wheel into a clean environment, and runs the bundled-library (import,
  resolve, initialize, report `BuildInfo`) and console-script smoke tests —
  all as one atomic step. Nothing under
  `src/rclone_kit/assets/native/` is ever written into this checkout; the
  tracked tree is byte-identical before and after the command, whether it
  succeeds or fails. `--out-dir` must be empty or nonexistent; omit it to
  let the script create a fresh temporary directory itself.

- **No source distribution is built or published.** Per the distribution
  policy in `docs/implementation_and_build_pipeline.md`, a normal `pip wheel`
  build from an sdist has no staging step and
  would silently produce a wheel without rclone. `rclone-kit` therefore
  publishes platform wheels only, until sdist-to-wheel builds are made
  complete and tested.

- **One `build_distribution.py` run produces one platform's wheel with no
  CPython ABI tag.** The in-tree build backend (`_build_backend.py`) forces
  a platform-tagged wheel (`win_amd64`, `manylinux2014_x86_64`) matching the
  *building* machine, with `py3`/`none` interpreter and ABI components — it
  does not cross-compile, and the script fails fast if `--target` does not
  match the host it is running on. A full release needs this command run
  once per certified platform — in practice, once per `wheel-windows` /
  `wheel-linux` job in `.github/workflows/ci.yml` — with every resulting
  wheel collected into one `dist/` directory before the final `uv publish`
  call. Once both are collected, run `uv run python
  scripts/verify_distribution.py dist --require-complete-release-set` to
  confirm the set is complete with no duplicates before publishing — this is
  exactly what CI's `release-assembly` job does after downloading both
  `wheel-windows-amd64` and `wheel-linux-amd64` artifacts.

- **The exact Python patch version is pinned in `.python-version`,** not
  just the `>=3.13` floor in `pyproject.toml`'s `requires-python`. `uv python
  install` with no argument (used throughout `.github/workflows/ci.yml`) and
  `build_distribution.py`'s smoke-test venv both resolve that same pinned
  patch version, so a release build never silently picks up a newer patch
  release mid-cycle.

## Release record

Every release must have a record — in the GitHub Release description, a
CHANGELOG entry, or equivalent — capturing:

- [ ] `rclone-kit` version (from `pyproject.toml`'s `[project] version`)
- [ ] Bundled rclone version (`rclone_upstream_version` in
      `native/toolchain.toml` plus the pinned `native/rclone` submodule
      commit; the value the shipped library itself reports is
      `NativeBuildInfo.rclone_version` — `src/rclone_kit/native/build_info.py`)
- [ ] Supported wheel platforms (the `wheel_platform_tag` values in
      `SUPPORTED_NATIVE_TARGETS`, `src/rclone_kit/runtime/native_platform.py`
      — e.g. `win_amd64`, `manylinux2014_x86_64`)
- [ ] Python version requirement (`requires-python` in `pyproject.toml`)
- [ ] Direct dependency changes since the previous release (diff
      `[project.dependencies]`, `[project.optional-dependencies]`, and
      `[dependency-groups]` against the prior tag)
- [ ] SHA-256 digests for every published wheel (`dist/*.whl` — `uv
      publish` prints these; `sha256sum dist/*` reproduces them)
- [ ] Known external mount prerequisites (WinFsp on Windows, FUSE on
      Linux). The library detects neither: `mount()` fails at the
      `mount/mount` RC call when the platform facility is absent. On
      Windows the bundled library carries mount support only when built
      with `-tags cmount`, which `--profile production` sets; on Linux
      `cmd/mount` needs no build tag, so every certified Linux build has
      it — see `scripts/native/build.py`'s `_build_tags` and `rc/mount.py`
- [ ] Version strings in `docs/production_usage.md`'s install block match
      the released version

## PyPI trusted publishing

`uv publish` must authenticate without a long-lived PyPI API token stored as
a repository secret. Use
[PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/) instead:

1. On PyPI, register a trusted publisher for the `rclone-kit` project
   pointing at this repository, `.github/workflows/release.yaml`, and the
   `pypi-release` GitHub Environment.
2. In this repository's GitHub settings, create that environment and add
   required reviewers (or another protection rule) so publishing needs
   explicit approval.
3. The publishing job requests a short-lived OIDC token via the
   `id-token: write` permission and `environment: pypi-release`; PyPI
   exchanges it for upload authorization. No `PYPI_API_TOKEN` (or
   equivalent) secret is ever stored in the repository or its environments.

`.github/workflows/release.yaml` runs for version tags matching `v*`, rejects
a tag whose value does not equal `v` plus the version in `pyproject.toml`,
repeats the quality and platform test gates, builds and verifies both
certified wheels, and publishes only the assembled `release-dist` artifact.
The `pypi-release` environment should require explicit maintainer approval.
