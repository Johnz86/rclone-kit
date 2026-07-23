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
