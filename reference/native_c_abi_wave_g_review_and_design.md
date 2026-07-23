# Native C ABI migration: Wave G design (direct filesystem facade)

Status: done

Date: 2026-07-24

Related documents:

- [Wave D review and design](native_c_abi_wave_d_review_and_design.md)
- [Wave F review and design](native_c_abi_wave_f_review_and_design.md)
- [CLI-to-C-ABI migration plan and ledger](rclone_cli_to_c_abi_migration_plan.md)

## 1. Scope

Ledger rows F01 (`filesystem()`), F02 (`cwd()`, transitive from F01), F03 (`RemoteFS.exists/is_file/
is_dir/ls`), F04 (`RemoteFS.read_bytes/write_binary/copy/remove`), F05 (`RemoteFS.dispose()`). T15
(`copy_file_s3_resumable`) is listed alongside this wave in the migration plan but is **not**
completable here - see section 4. D14/D15 are, as with D10/D11 in Wave F, this migration plan's own
internal/distribution-removal ledger (their gate is "F01–F05 pass"/"F06 passes"), not action items.

## 2. What was found before changing anything

Reading `src/rclone_kit/fs/filesystem.py` before touching it showed the actual scope was narrower
than "rewrite RemoteFS":

- `RemoteFS.read_bytes`/`write_binary`/`copy`/`remove` already call `self.rclone.read_bytes()`/
  `write_bytes()`/`copy_to()`/`delete_files()` - the public `Rclone` methods, already embedded-
  capable since Waves D/E/F. **F04 needed no changes at all** once F01/F03 removed the server
  dependency these methods never actually had.
- Only `exists`/`is_dir`/`is_file`/`ls` used `self.server` (`assert isinstance(self.server,
  HttpServer)`), backed by HTTP HEAD/autoindex parsing. These four were the actual F03 target.
- `RemoteFS.__init__` unconditionally called `self.rclone.serve_http(src=src)` - exactly F01's
  complaint ("constructing a filesystem must bind no port and start no server").
- `tests/cloud/test_fs_remote.py` already has a **known, currently-skipped bug** on record:
  `test_create_and_remove_remote_fs` is marked `@unittest.skip` because `RemoteFS.exists()` (HTTP-
  autoindex-backed) reported a just-deleted file as still present, "presumably due to a caching
  layer between the HTTP listing and the actual remote delete." Routing `exists()` through direct
  `operations/stat` instead bypasses that autoindex cache entirely, so this rewrite may incidentally
  fix that pre-existing bug - plausible, not yet confirmed, since it needs a live bucket this
  environment cannot reach. Left as a note for whoever next runs the cloud suite, not re-enabled
  blind.

## 3. Design decisions

### G1 - No implicit server; an explicit, lazy `serve()` instead

`RemoteFS.__init__` no longer calls `serve_http()`. A new `RemoteFS.serve(addr=None,
other_args=None)` method starts (or returns the already-started) server on first explicit call - for
a future consumer that genuinely needs a real `HttpServer` (multipart/resumable downloads, Wave H),
never implicitly. No other code in this repository currently reads `RemoteFS.server` besides
`RemoteFS` itself, confirmed by search before deciding this was safe to make lazy rather than keeping
some other required eager-construction path.

### G2 - `exists`/`is_dir`/`is_file`/`ls` go straight through the `Rclone` facade

- `exists(path)` → `self.rclone.exists(path)` (already embedded-capable: `operations/stat`).
- `is_dir(path)`/`is_file(path)` → `self.rclone.stat(path).path.is_dir`, catching `FileNotFoundError`
  to return `False` (matching the previous HTTP-based catch-and-return-`False` shape, not a new
  contract).
- `ls(path)` → `self.rclone.ls(path, max_depth=0)`, converting the returned `DirListing` into the
  exact same `(files, dirs)` bare-name-string shape the old HTTP-autoindex-backed version returned -
  including directory names keeping a trailing `/` marker, since `FSPath.lspaths()`/`fs_walk` are
  documented as depending on that exact `RemoteFS`-vs-`RealFS` asymmetry (see `FS.ls`'s own
  docstring). A missing path now raises `FileNotFoundError` via the same broad-except-and-translate
  pattern `read_bytes()`/`copy()` already used elsewhere in this class, not a new error-handling
  style.

`path` arguments to all four are **already the full remote path string** (e.g. `"remote:bucket/
sub/file.txt"`, built by `FSPath.__truediv__`/`RemoteFS.root()`), not relative to `self.src` - unlike
the old HTTP-server-backed versions, which had to compute a path relative to the server's own served
root (`_to_remote_path`). `Rclone.exists()`/`stat()`/`ls()` take full paths directly, so that
conversion is no longer needed for these four methods (it is still needed, and unchanged, by
`copy()`, which addresses `is_s3()` by a path relative to `self.src`).

### G3 - `RemoteFSAccess` protocol gains `exists`/`stat`/`ls`, keeps `serve_http`

The protocol `RemoteFS` depends on gained the three methods G2 needs; `serve_http` stays for G1's
explicit `serve()`. `Rclone` already implements all of these regardless of execution mode, so no
change was needed to `Rclone` itself - only to the protocol declaration and `RemoteFS`'s body.

## 4. T15 (`copy_file_s3_resumable`) is genuinely blocked on Wave H, not done here

Read before assuming: `copy_file_parts_resumable()` → `upload_parts_resumable()` (`s3/multipart/
upload_parts_resumable.py:298`) calls `self.serve_http(src_dir)` as a context manager - a real,
load-bearing HTTP-server dependency inside the resumable-upload flow itself (`MultipartAccess`'s own
protocol declares `serve_http` for exactly this reason). `serve_http()` is not embedded-capable yet
(Wave H, ledger rows R01-R05/D12-D13, has not been done) - calling it under `execution="embedded"`
correctly raises `UnsupportedEmbeddedOperationError` today, per the no-silent-fallback invariant. The
migration plan's own T15 row already predicted this exact dependency ("Depends on list/read/write/
copy/serve rows migrating first"); Wave G completed the list/read/write/copy side, but "serve" is
Wave H's job, not this one's. `tests/parity/coverage.toml`'s T15 row stays `planned`, with its notes
updated to name the specific blocking call site rather than left as a generic "depends on" pointer.

## 5. Test coverage plan

`RemoteFS` is backend-agnostic (only depends on the `RemoteFSAccess` protocol `Rclone` satisfies
regardless of execution mode or whether `src` is a cloud remote or a local path), so native parity
tests exercise it against local temp directories through both an embedded and a CLI-backed `Rclone`,
without needing live cloud credentials: construction starts no server; `exists`/`is_dir`/`is_file`
for present/missing/directory/file targets; `ls` matching CLI output including the trailing-slash
directory marker; `ls` raising `FileNotFoundError` for a missing path; `read_bytes`/`write_binary`/
`copy`/`remove` still working end-to-end through `FSPath`; `dispose()` being a true no-op when no
server was ever started. Unit tests with a fake `RemoteFSAccess` cover the same dispatch logic
without a built native library.
