# Path and remote-resolution refactor plan (resolved)

Follow-up to the Linux local-path fixes on `native-c-abi-migration`
(`33a6485` "Fix two more Linux-only local-path bugs found by the
wheel-linux job"). That commit fixed a Linux-only symptom; a review
afterward found the same code had an unfixed Windows symptom, that the
underlying remote:path parsing logic was duplicated three times with
three different levels of correctness, and that some of the code
exercising these bugs was itself dead - reachable only from the test
suite, not from any real feature. All items below are resolved, each in
its own commit on `native-c-abi-migration`:

## 1. `group_files.parse_file`/`group_files` never learned to split on `\`

RESOLVED (`a3d13d9`, `b48edc8`). `RcPath.parse_parts`
(`src/rclone_kit/rc/paths.py`) adds the `remote`/`parents`/`name`
decomposition primitive: a real remote path splits on `PurePosixPath`
(rclone remote paths are always forward-slash, regardless of host OS);
a Windows drive-letter path splits on `PureWindowsPath` explicitly - not
the host's native `Path` - so the decomposition is deterministic on any
host OS, not only when actually running on Windows.
`group_files.parse_file` now delegates to it instead of hand-splitting,
closing the Windows grouping gap
(`tests/unit/test_group_files.py::test_fully_qualified_windows_local_path_splits_on_backslash`
is the regression test). `_colonify` also needed to keep a separator
after a drive letter's colon (`C:/Users`, not the drive-relative
`C:Users`) when reassembling the group key.

## 2. The same "split `remote:path`" problem was solved three times, with three different levels of correctness

RESOLVED (`a3d13d9`, `a8171df`). `RcPath.parse`/`RcPath.parse_parts`
(`rc/paths.py`) is now the single source of truth.
`util.split_remote_name_and_path` - whose own docstring already admitted
it didn't special-case a Windows drive letter - is deleted; its three
call sites (`util.to_path`, and `listing_ops_embedded.py`'s
`_stat_item`/`fetch_size_files_embedded`) now use
`rc.paths.split_remote_and_path`.

## 3. `FSPath.move_to`/`set_owner`/`is_real_fs` were dead production code

RESOLVED (`7401d3d`) by deletion, not by wiring them up. `move_to` had
exactly two callers in the whole repository, both tests; `Rclone` has
no `move`/`move_file` API and no `operations/movefile` RC plumbing
anywhere, so it was never reachable from a real feature. `set_owner`/
`is_real_fs` had zero callers anywhere, including tests - `set_owner`
was the only way `FSPath.fs_holder` ever became non-`None`, so the
"dispose the filesystem I own on context exit" logic in
`__enter__`/`__exit__` could never fire (`Rclone.cwd()` returns exactly
such an unowned `FSPath`, so `with rclone.cwd(src) as cwd: ...` already
never disposed the underlying `RemoteFS` - a real resource-cleanup gap
this dead code was masking, not preventing). Building a genuine move
feature (backed by `operations/movefile`, with job-monitor integration
matching `copy`/`delete_files`, its own tests and docs) would be a real
feature addition on the scale of the existing `copy`/`delete_files`
ports - out of scope here. The two test call sites now use
`fs.copy(...)` + `fs.remove()` directly, what `move_to` did internally.

## 4. `assert` used for input/state validation in business logic, not fixed invariants

RESOLVED:

- `FSPath.rmtree` (`bc242ab`): raises `FileNotFoundError` instead of
  asserting, matching every sibling method in `filesystem.py`.
- `group_files.group_under_remote_bucket` (`0eec65a`): raises
  `NotImplementedError` for `fully_qualified=False` instead of
  asserting; this function had zero test coverage before, now has tests
  for both the normal path and the rejection.
- `client.py`'s 17 `assert self._X is not None` occurrences (`9735aa0`):
  `_rc_client` and `_embedded_runtime` are unconditionally assigned a
  real value once in `__init__` and never reset to `None` afterward
  (including in `close()`), so the `| None` typing was never a real
  runtime guard - just narrowing boilerplate duplicated 17 times.
  Dropped the `| None` typing entirely instead of reformulating the
  asserts as exceptions; `pyright` confirms no new narrowing errors
  result.
- The same "assert on parsed/computed state" pattern in
  `s3/chunk_task.py`, `s3/multipart/upload_parts_server_side_merge.py`,
  `s3/multipart/upload_state.py` was out of scope for this plan (S3
  multipart state, not path/remote resolution) and remains unstarted -
  worth a follow-up note if that area gets touched next.

**Next step**: nothing outstanding from this plan. Delete this file
whenever it stops being a useful pointer to the above commits.
