# Release process

This document covers the exact release command sequence, what a release
record must capture, and how PyPI publishing is authorized without a stored
long-lived token.

## Release command sequence

Run from a clean checkout of the commit being released, on each certified
target platform (currently Windows AMD64 and Linux AMD64 — see
`src/rclone_kit/runtime/platform.py`'s `SUPPORTED_ARTIFACTS`):

```powershell
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run pyright src tests scripts
uv run pytest tests/unit
uv run pytest tests/integration
uv build
uv run python scripts/verify_distribution.py dist
uv publish --check-url https://pypi.org/simple
```

Two details matter when actually executing it:

- **`uv build` needs a staged rclone artifact first.** Nothing under
  `src/rclone_kit/assets/rclone/` is committed (see `.gitignore`), so before
  `uv build` runs, stage the certified artifact for the platform you are
  building on:

  ```powershell
  uv run python scripts/prepare_rclone_artifact.py <windows|linux> amd64 --out-dir build/rclone-artifacts
  Copy-Item -Path build/rclone-artifacts/rclone/* -Destination src/rclone_kit/assets/rclone -Recurse -Force
  ```

  This mirrors exactly what `.github/workflows/ci.yml`'s `package` job does
  on each matrix runner.

- **Build the sdist before staging, the wheel after.** A single `uv build`
  invocation shares its file-inclusion logic between the sdist and the
  wheel closely enough that excluding `assets/rclone/` from just the sdist
  via `MANIFEST.in` also strips it from the wheel. The reliable split is:

  ```powershell
  uv build --sdist    # before staging: sdist stays platform-independent
  # ... stage the rclone artifact as shown above ...
  uv build --wheel     # after staging: wheel bundles the executable
  ```

  `scripts/verify_distribution.py` enforces the sdist side of this
  invariant (`check_sdist_has_no_staged_platform_executables`) so a release
  fails closed if the ordering above is skipped.

- **One `uv build` run produces one platform's wheel.** The in-tree build
  backend (`_build_backend.py`) forces a platform-tagged wheel matching the
  *building* machine; it does not cross-compile. A full release needs this
  sequence run once per certified platform — in practice, once per leg of
  `.github/workflows/ci.yml`'s `package` matrix — with every resulting
  wheel collected into one `dist/` directory (alongside the single,
  platform-independent sdist) before the final `uv run python
  scripts/verify_distribution.py dist` and `uv publish` calls.

## Release record

Every release must have a record — in the GitHub Release description, a
CHANGELOG entry, or equivalent — capturing:

- [ ] `rclone-kit` version (from `pyproject.toml`'s `[project] version`)
- [ ] Bundled rclone version (`RCLONE_VERSION` in
      `src/rclone_kit/runtime/platform.py`)
- [ ] Supported wheel platforms (the `wheel_platform_tag` values in
      `SUPPORTED_ARTIFACTS`, e.g. `win_amd64`, `manylinux2014_x86_64`)
- [ ] Python version requirement (`requires-python` in `pyproject.toml`)
- [ ] Direct dependency changes since the previous release (diff
      `[project.dependencies]` and `[dependency-groups]` against the prior
      tag)
- [ ] SHA-256 digests for every published file (`dist/*.whl`, `dist/*.tar.gz`
      — `uv publish` prints these; `sha256sum dist/*` reproduces them)
- [ ] Known external mount prerequisites (WinFsp on Windows, FUSE plus a
      usable unmount command on Linux — see `rclone_kit.mount_util`'s
      availability checks)

## PyPI trusted publishing

`uv publish` must authenticate without a long-lived PyPI API token stored as
a repository secret. Use
[PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/) instead:

1. On PyPI, register a trusted publisher for the `rclone-kit` project
   pointing at this repository, the workflow file that will run `uv
   publish`, and a specific GitHub Environment name (for example
   `pypi-release`).
2. In this repository's GitHub settings, create that environment and add
   required reviewers (or another protection rule) so publishing needs
   explicit approval.
3. The publishing job requests a short-lived OIDC token via the
   `id-token: write` permission and `environment: pypi-release`; PyPI
   exchanges it for upload authorization. No `PYPI_API_TOKEN` (or
   equivalent) secret is ever stored in the repository or its environments.

This repository does not yet include a `release.yml` workflow that performs
the publish step; that was a deliberate choice rather than something added
speculatively. The sequence above is the one a maintainer runs by hand (or
wires into such a workflow later) until trusted publishing is registered on
PyPI's side for this project.
