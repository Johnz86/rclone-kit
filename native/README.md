# `native/`

`native/rclone` is a Git submodule pinned to a commit of the maintained
rclone fork (`https://github.com/Johnz86/rclone.git`). It is the only
production build source for rclone-kit's native artifacts.

`reference/rclone` (outside this directory) is a separate, unpinned research
checkout used for inspection and design notes. It must never be used as a
build input.

## Branch roles in the fork

- `master`: tracks upstream `rclone/rclone` `master` without downstream
  product commits.
- `oauth-public-redirect`: the smallest upstreamable listener/redirect
  change and its tests. The eventual upstream pull request is created from
  this branch.
- `rclone-kit/integration-v1`: a release-based integration branch containing
  the tested OAuth commit plus the rclone-kit downstream C bridge
  (`librclone/rclonekit`). This is the branch rclone-kit actually pins and
  builds from.

## Updating the pin

1. Work and test Go changes in a `native/rclone` worktree on the relevant
   fork branch; commit and push them to the fork.
2. In the parent `rclone-kit` repository, `git -C native/rclone checkout
   <commit-or-branch>` then `git add native/rclone` to move the pin.
3. Commit the updated submodule pointer together with the Python/build
   changes that consume it.

Never leave the parent pointing at a Go commit that exists only in a local
worktree; CI verifies the pinned commit is fetchable from `origin` on the
configured fork remote.

## `native/toolchain.toml`

Records inputs the Git commit pin does not represent: the required Go
version, the rclone-kit C ABI version, the fork URL and branch names, the
Windows compiler, and the Linux wheel compatibility policy. Build scripts
must fail clearly when the resolved toolchain disagrees with this file.

### Windows compiler note

The system MinGW-w64 GCC (w64devkit, GCC 14.2.0/binutils 2.43 at
`C:\Programs\w64devkit\bin\gcc.exe`) builds plain Go executables fine but
**cannot** produce a working `-buildmode=c-shared`/`c-archive` artifact on
this machine: its assembler unconditionally emits COFF objects in the
"bigobj" format, which Go's cgo tooling (`cmd/cgo`, backed by `debug/pe`)
cannot parse (`cgo: cannot parse gcc output ... as ELF, Mach-O, PE, XCOFF
object`). This reproduces identically building upstream's own unmodified
`librclone` package, so it is an environment/toolchain issue, not an
rclone-kit code defect.

The working substitute is [llvm-mingw](https://github.com/mstorsjo/llvm-mingw)
(a matched clang+lld+mingw-w64 distribution that avoids GNU `as` entirely),
installed at `C:\Programs\llvm-mingw` (release `20260616`, `ucrt-x86_64`
variant). Use its target-prefixed wrapper as `CC` for any cgo `c-shared`/
`c-archive` build:

```powershell
$env:CC = "C:\Programs\llvm-mingw\bin\x86_64-w64-mingw32-gcc.exe"
```

Verified: builds both upstream `librclone` and `librclone/rclonekit` as a
working `.dll`, loadable and callable repeatedly from Python `ctypes`
without leaks or crashes. Plain (non-cgo) executable builds may continue
using either compiler; `go build .` for `rclone.exe` was not affected by
the bigobj issue.

## Linux build (local Docker verification)

Linux native builds must happen inside the pinned `quay.io/pypa/manylinux2014_x86_64` container
(matching `native/toolchain.toml`'s `linux_wheel_policy`), not on a bare Linux host: only that image
gives a genuinely manylinux2014-compatible glibc baseline. This section is the exact, reproducible
command sequence used to verify the whole native-library packaging pipeline for Linux locally, via
Docker Desktop (WSL2 backend) on a Windows development machine - no new Docker image was built; only
the standard, already-published PyPA image, with tools installed into a running container.

Unlike Windows, no build-time WinFsp-equivalent SDK is needed at all for Linux mount support: see
`native_c_abi_wave_h_review_and_design.md`'s Linux mount addendum for why `cmd/mount` (bazil.org/fuse,
pure Go) needs no cgo or system FUSE headers to compile.

### 1. Start a persistent container with the repo mounted and FUSE devices enabled

```bash
# From a bash shell (Git Bash on Windows needs MSYS_NO_PATHCONV=1 so /dev/fuse
# and bash -c "..." arguments are not mangled into Windows paths).
docker pull quay.io/pypa/manylinux2014_x86_64

MSYS_NO_PATHCONV=1 docker run -d --name rclone-kit-linux-build \
  --device /dev/fuse --cap-add SYS_ADMIN \
  -v /d/GIT/python/rclone-kit:/work \
  quay.io/pypa/manylinux2014_x86_64 sleep infinity
```

`--device /dev/fuse --cap-add SYS_ADMIN` are required to actually mount FUSE filesystems inside the
container (needed only for the real mount verification in step 4, not for a plain build).

### 2. Install the pinned Go toolchain (not present in the base image)

```bash
docker exec rclone-kit-linux-build bash -c "
  curl -fsSL -o /tmp/go.tar.gz https://go.dev/dl/go1.26.5.linux-amd64.tar.gz &&
  tar -C /usr/local -xzf /tmp/go.tar.gz &&
  /usr/local/go/bin/go version
"
```

The image already provides a devtoolset gcc (10.2.1 as pulled) active on `PATH` for every shell with
no extra activation step, and `auditwheel`/`uv` preinstalled. No `fuse-devel`/`pkg-config fuse`
package is needed at build time.

### 3. Build the native library directly

```bash
docker exec -e PATH=/usr/local/go/bin:$PATH rclone-kit-linux-build bash -c "
  cd /work/native/rclone &&
  go build -trimpath -buildvcs=true -buildmode=c-shared \
    -o /tmp/librclone_kit.so ./librclone/rclonekit
"
```

Verify no `libfuse.so` dependency was pulled in, and inspect the referenced glibc symbol versions
(should be well under manylinux2014's 2.17 baseline):

```bash
docker exec rclone-kit-linux-build bash -c "
  ldd /tmp/librclone_kit.so
  objdump -T /tmp/librclone_kit.so | grep -oE 'GLIBC_[0-9.]+' | sort -Vu
"
```

### 4. Verify a real mount works (needs the `fuse3` runtime package)

`bazil.org/fuse` shells out to `fusermount3`/`fusermount` for the kernel handshake even with
`CAP_SYS_ADMIN`, so install the runtime package first:

```bash
docker exec rclone-kit-linux-build bash -c "yum install -y fuse3 fuse"
```

Then drive the real C ABI directly via `ctypes` (mirrors exactly how production Python code loads
the library - see `rclone_kit/native/abi.py` for the authoritative signatures) to mount a local
directory, read a file through it, list it, and unmount it. This is the same verification method
used for the Windows WinFsp toolchain (see the Wave H design doc's mount addendum).

### 5. Build, stage, verify, and smoke-test the actual wheel end to end

```bash
MSYS_NO_PATHCONV=1 docker exec \
  -e PATH=/usr/local/go/bin:/opt/rh/devtoolset-10/root/usr/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  -e UV_PROJECT_ENVIRONMENT=/tmp/venv-linux \
  -w /work rclone-kit-linux-build bash -c "
  uv run --python 3.13 python scripts/build_distribution.py --target linux-amd64 --out-dir /tmp/dist_linux
"
```

`UV_PROJECT_ENVIRONMENT` points `uv` at a container-local venv path instead of the mounted
`/work/.venv`, which is a Windows-created venv (via the bind mount) that `uv` cannot safely
recreate for Linux from inside the container. This is the full pipeline
`scripts/build_distribution.py` already runs for Windows: builds the native library with
`--profile production`, stages it into `assets/native/manylinux2014_x86_64/`, builds the wheel,
runs every `scripts/verify_distribution.py` check (including `auditwheel`-equivalent glibc/mount
checks), installs it into a clean venv with no dev dependencies, and smoke-tests it - confirming the
bundled library resolves, initializes, and reports real `BuildInfo`, with the same network-isolated
smoke test used for Windows.

Confirm official manylinux compatibility with `auditwheel` itself:

```bash
docker exec rclone-kit-linux-build bash -c "
  auditwheel show /tmp/dist_linux/rclone_kit-1.0.0-py3-none-manylinux2014_x86_64.whl
"
```

Verified result: `auditwheel` reports the wheel is consistent with the even stricter
`manylinux_2_5_x86_64` tag and "requires no external shared libraries" - comfortably inside the
declared `manylinux2014_x86_64` policy.

### Known quirk: spurious `vcs.modified=true`/`-dirty` on Windows

On this machine (`core.autocrlf=true`), `go build -buildvcs=true`'s own
embedded VCS stamp reports `vcs.modified=true` (surfacing as a `-dirty`
suffix in `RcloneKitBuildInfo`'s `rcloneCommit`) even immediately after a
clean checkout, verified independently clean by `git status --porcelain`,
`git diff --name-status HEAD`, and `git ls-files --others --exclude-standard`
all returning empty. This reproduces across a forced `-a` rebuild and with
the parent `rclone-kit` checkout itself stashed clean, so it is not caused by
the parent superproject's state. It is a known Go/git tooling interaction
with CRLF line-ending conversion, not a real dirty submodule. Treat
`scripts/native/build.py`'s own `native-manifest.json` `fork.worktree_clean`
field (computed directly from `git status --porcelain`) as authoritative;
do not use the DLL's self-reported build info to judge worktree cleanliness.
