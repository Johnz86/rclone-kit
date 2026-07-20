# Production usage

This guide describes how to deploy and operate `rclone-kit` as an application
dependency. It focuses on the public API exported from `rclone_kit` and on
resource ownership, failure handling, and verification patterns that matter in
long-running services.

## Supported runtime

`rclone-kit` 1.0.0 requires Python 3.13 or newer. Published wheels support:

- Windows amd64;
- Linux amd64 using the `manylinux2014_x86_64` platform tag.

Each supported wheel contains a pinned, checksum-verified rclone executable.
A wheel installation therefore does not need a system rclone installation or
`PATH` configuration.

Pin the application dependency in production:

```text
rclone-kit==1.0.0
```

Install only the optional features the process uses:

```bash
pip install "rclone-kit==1.0.0"
pip install "rclone-kit[s3]==1.0.0"
pip install "rclone-kit[database]==1.0.0"
pip install "rclone-kit[postgres]==1.0.0"
pip install "rclone-kit[full]==1.0.0"
```

The `s3` extra adds direct and multipart S3 support. The `database` extra adds
SQLite and database inventory support, while `postgres` also installs the
PostgreSQL driver. `full` installs every optional feature.

The bundled executable is copied into a per-user application cache and
verified again at runtime. Containers should give their runtime user a
writable home or cache directory. Avoid running the same image under an
ephemeral, read-only home unless the cache is prepared elsewhere.

## Configuration

### Mount a configuration file as a secret

For a service, the preferred arrangement is a read-only `rclone.conf` supplied
by the deployment platform:

```python
from pathlib import Path

from rclone_kit import Rclone

CONFIG_PATH = Path("/run/secrets/rclone.conf")

rclone = Rclone(CONFIG_PATH)
```

On Windows, use the corresponding absolute `Path`. Construction fails early
when an explicitly supplied file does not exist.

Alternatively, set `RCLONE_CONFIG` and allow standard discovery:

```python
from rclone_kit import Rclone

rclone = Rclone(None)
```

Discovery checks `RCLONE_CONFIG` and then asks rclone for its active config
path. A warning is emitted when no config can be found, so applications should
treat that warning or a failed startup probe as a deployment error.

### Build configuration in memory

`Config` accepts rclone configuration text or a dictionary:

```python
import os

from rclone_kit import Config, Rclone

config = Config.from_json(
    {
        "archive": {
            "type": "s3",
            "provider": "DigitalOcean",
            "access_key_id": os.environ["OBJECT_STORAGE_ACCESS_KEY"],
            "secret_access_key": os.environ["OBJECT_STORAGE_SECRET_KEY"],
            "endpoint": os.environ["OBJECT_STORAGE_ENDPOINT"],
        }
    }
)
rclone = Rclone(config)
```

When a `Config` object is used, the library creates a private temporary config
file for each rclone process and removes it after use. Do not print the
`Config`, place credentials in source control, or pass secrets through
`other_args`. Command logging redacts recognized credential flags, but the
safest production pattern is still a secret-backed config file.

Use rclone's obscured password format when a backend requires it:

```python
obscured_password = rclone.obscure(os.environ["SFTP_PASSWORD"])
```

Obscuring is compatible encoding, not encryption. Protect the resulting value
as a secret.

### Verify configuration during startup

Fail before accepting work if required remotes are absent:

```python
from rclone_kit import Rclone


def verify_storage(client: Rclone, required_remotes: set[str]) -> None:
    configured = {remote.name for remote in client.listremotes()}
    missing = required_remotes - configured
    if missing:
        names = ", ".join(sorted(missing))
        raise RuntimeError(f"Missing rclone remotes: {names}")


verify_storage(rclone, {"archive", "source"})
```

`config_paths()` returns the config, cache, and temporary paths reported by
rclone, in that order. `config_show()` is useful for diagnostics, but its
output can contain secrets; never include it in routine production logs.

## Paths and result objects

Remote paths use rclone syntax:

```text
remote:bucket/prefix/file.ext
```

Local paths use normal operating-system paths. Do not invent a `local:`
remote unless one is actually defined in the rclone config.

Short-lived operations return `CompletedProcess` where the command result is
useful:

```python
result = rclone.copy(
    "/srv/incoming",
    "archive:training-data/incoming",
    check=True,
)
if not result.ok:
    raise RuntimeError(result.stderr)
```

Set `check=True` on copy operations when command failure should immediately
raise. For partitioned operations, inspect every returned result:

```python
results = rclone.copy_files(
    src="source:dataset",
    dst="archive:dataset",
    files=["images/0001.png", "labels/0001.json"],
    check=True,
)
if not all(result.ok for result in results):
    raise RuntimeError("One or more copy partitions failed")
```

## Listing and metadata

### List one directory

`ls()` returns a `DirListing` with separate `dirs` and `files` collections:

```python
from rclone_kit import ListingOption, Order

listing = rclone.ls(
    "archive:training-data/images",
    max_depth=0,
    glob="*.png",
    order=Order.NORMAL,
    listing_option=ListingOption.FILES_ONLY,
)

for file in listing.files:
    print(file.name, file.size, file.mod_time_dt())
```

`max_depth=0` lists the immediate path. A negative depth requests recursive
listing. `glob` is applied to returned paths by the Python client.

Other metadata helpers cover common probes:

```python
from rclone_kit import SizeSuffix

path = "archive:training-data/manifest.json"

if rclone.exists(path):
    modified_at = rclone.modtime_dt(path)
    size: SizeSuffix = rclone.size_file(path)
    print(modified_at, size.as_int())
```

`stat()` and `size_file()` raise `FileNotFoundError` for a missing file.
`size_file()` also raises `ValueError` when the path matches more than one
file.

For a selected group, `size_files()` returns the aggregate and individual
sizes without listing unrelated objects:

```python
result = rclone.size_files(
    src="archive:training-data",
    files=["images/0001.png", "labels/0001.json"],
    check=True,
)

print(result.total_size)
for relative_path, size in result.file_sizes.items():
    print(relative_path, size)
```

File names passed to `size_files()` are relative to `src`.

### Stream very large inventories

Use `ls_stream()` when a recursive inventory should not be materialized in
memory:

```python
with rclone.ls_stream(
    "archive:training-data",
    max_depth=-1,
    fast_list=False,
) as stream:
    for page in stream.files_paged(page_size=10_000):
        persist_inventory_page(page)
```

Always use the context manager so the underlying process is terminated and
its temporary configuration is removed if iteration stops early.

`fast_list=True` reduces backend transactions on remotes where rclone
supports it, but can consume much more memory because rclone loads a full
recursive listing. Enable it only after measuring the target backend and
inventory size.

### Walk directory by directory

`walk()` yields one `DirListing` per visited directory:

```python
for directory in rclone.walk(
    "archive:training-data",
    max_depth=2,
    breadth_first=True,
):
    for file in directory.files:
        print(file.to_string())
```

Set `breadth_first=False` for depth-first traversal. Use
`scan_missing_folders()` when only missing directory structure matters:

```python
for missing_dir in rclone.scan_missing_folders(
    src="source:dataset",
    dst="archive:dataset",
):
    print(missing_dir)
```

When comparing source and destination, make their roots semantically
equivalent. An off-by-one parent directory produces misleading differences.

## Reading and writing objects

Small control files can be handled directly:

```python
import json

manifest_path = "archive:jobs/run-42/manifest.json"

rclone.write_text(
    json.dumps({"run_id": 42, "state": "ready"}),
    manifest_path,
)
manifest = json.loads(rclone.read_text(manifest_path))
```

`read_bytes()` and `write_bytes()` are convenient for small payloads, but
they buffer the complete object in memory and use a temporary local file.
Use transfer methods or HTTP downloads for large data.

Read a byte range directly to disk:

```python
from pathlib import Path

from rclone_kit import SizeSuffix

rclone.copy_bytes(
    src="archive:models/model.bin",
    offset=SizeSuffix("64M"),
    length=SizeSuffix("8M"),
    outfile=Path("/srv/work/model.part"),
)
```

`SizeSuffix` accepts values such as `16K`, `1.5M`, and `2G`, and can be
converted with `as_int()` or `as_str()`.

## Transfer workflows

### Copy a tree or one file

`copy()` uses tuned defaults for recursive transfers and does not delete
objects that exist only at the destination:

```python
result = rclone.copy(
    src="/srv/incoming/run-42",
    dst="archive:runs/run-42",
    check=True,
    transfers=32,
    checkers=256,
    low_level_retries=10,
    retries=3,
)
assert result.ok
```

Use `copy_to()` when both source and destination name one file:

```python
result = rclone.copy_to(
    "/srv/incoming/manifest.json",
    "archive:runs/run-42/manifest.json",
    check=True,
)
assert result.ok
```

Pass only reviewed rclone flags in `other_args`. Values are executed as an
argument vector with `shell=False`, but flags can still materially change
rclone behavior.

### Copy a selected file set

Names are relative to the supplied source root and must not include a remote
prefix:

```python
selected = [
    "images/0001.png",
    "images/0002.png",
    "labels/index.json",
]

results = rclone.copy_files(
    src="source:dataset",
    dst="archive:dataset",
    files=selected,
    check=True,
    max_partition_workers=4,
    transfers=16,
    checkers=128,
    retries=3,
    retries_sleep="5s",
    timeout="10m",
)
```

`files` may also be a `Path` to a newline-delimited file list.
`max_partition_workers` creates independent rclone commands grouped by common
prefix; it multiplies total transfer concurrency, so tune it together with
`transfers`.

### Verify before cleanup

A conservative move workflow copies, verifies, and only then deletes:

```python
source = "source:completed/run-42"
destination = "archive:completed/run-42"

copy_result = rclone.copy(source, destination, check=True)
if not copy_result.ok:
    raise RuntimeError(copy_result.stderr)

if not rclone.is_synced(source, destination):
    raise RuntimeError("Destination verification failed")

purge_result = rclone.purge(source)
if not purge_result.ok:
    raise RuntimeError(purge_result.stderr)
```

`purge()` is destructive: it removes the path and all contents. Keep it
behind application-level authorization and test path construction before
enabling it in a production worker.

For selected files, `delete_files()` accepts one path or a list of fully
qualified remote paths:

```python
result = rclone.delete_files(
    [
        "source:dataset/stale/0001.bin",
        "source:dataset/stale/0002.bin",
    ],
    check=True,
    rmdirs=True,
)
if not result.ok:
    raise RuntimeError(result.stderr)
```

## Streaming differences and reconciliation

`diff()` streams comparison results while rclone is still running:

```python
from rclone_kit import DiffOption, DiffType

missing = rclone.diff(
    src="source:dataset",
    dst="archive:dataset",
    diff_option=DiffOption.MISSING_ON_DST,
    fast_list=True,
    size_only=True,
    checkers=256,
)

for item in missing:
    assert item.type is DiffType.MISSING_ON_DST
    enqueue_copy(item.src_path(), item.dst_path())
```

Use `DiffOption.COMBINED` to receive equal, missing, different, and error
records in one stream. `min_size` and `max_size` accept rclone size strings
such as `"10M"`. The default `fast_list=True` is optimized for full-tree
comparisons; disable it if its memory use is unsuitable for the inventory.

## Filesystem-style remote access

`RemoteFS` and `FSPath` offer a small `pathlib`-like interface backed by a
scoped local HTTP server:

```python
with rclone.filesystem("archive:jobs") as remote_fs:
    root = remote_fs.cwd()
    job = root / "run-42"
    manifest = job / "manifest.json"

    if manifest.exists():
        print(manifest.read_text())

    output = job / "worker-result.json"
    output.write_text('{"status":"complete"}')

    with job.walk_begin(max_backlog=8) as walker:
        for current, dirnames, filenames in walker:
            print(current, dirnames, filenames)
```

Scope `RemoteFS` with `with`, especially in web services and workers. It owns
an rclone HTTP process. `FSPath.write_bytes()` buffers its input, and remote
`mkdir()` is not supported because object stores usually represent
directories as prefixes.

Local paths can use the same interface without launching rclone:

```python
from pathlib import Path

from rclone_kit import FSPath

local = FSPath.from_path(Path("/srv/work"))
for current, dirnames, filenames in local.walk():
    print(current, dirnames, filenames)
```

## Scoped HTTP downloads

`serve_http()` is useful for repeated reads, parallel downloads, and byte
ranges:

```python
from pathlib import Path

from rclone_kit import Range, SizeSuffix

with rclone.serve_http("archive:models") as server:
    remote_name = "releases/model-v4.bin"
    print(server.size(remote_name))

    server.download_multi_threaded(
        src_path=remote_name,
        dst_path=Path("/srv/models/model-v4.bin"),
        chunk_size=SizeSuffix("32M").as_int(),
        n_threads=8,
    )

    header = server.get(
        remote_name,
        range=Range(start=0, end=SizeSuffix("4K")),
    )
```

The `Range` end is exclusive. The server context manager shuts down the
process even if a download raises. Bind to the automatically selected
localhost port unless another process must reach the endpoint. If a fixed
address is required, restrict it with host firewall and deployment network
policy.

## Mounts and WebDAV

Mounts require FUSE on Linux or WinFsp on Windows. Use a context manager so
unmounting and optional cache cleanup happen on every exit path:

```python
from pathlib import Path

mount_path = Path("/mnt/archive")
cache_path = Path("/var/cache/my-service/rclone-vfs")

with rclone.mount(
    src="archive:datasets",
    outdir=mount_path,
    allow_writes=False,
    vfs_cache_mode="full",
    cache_dir=cache_path,
    cache_dir_delete_on_exit=False,
    transfers=32,
) as mounted:
    consume_files(mounted.mount_path)
```

For object storage, `mount_s3()` supplies S3-oriented VFS defaults:

```python
with rclone.mount_s3(
    url="archive:datasets",
    outdir=Path("/mnt/archive"),
    allow_writes=False,
    vfs_cache_mode="full",
    vfs_disk_space_total_size="20G",
) as mounted:
    consume_files(mounted.mount_path)
```

Mounts are operational infrastructure: provision disk for the VFS cache,
monitor its utilization, and run mount-specific smoke tests on the target OS.

`serve_webdav()` returns a long-lived `Process`. Bind it to a private
interface, require credentials, and scope it:

```python
with rclone.serve_webdav(
    src="archive:shared",
    user="service-user",
    password=webdav_password,
    addr="127.0.0.1:9080",
) as process:
    assert process.poll() is None
    run_consumer()
```

## S3-optimized operations

Install the `s3` extra before using these methods.

For a local file below the backend's normal multipart threshold, upload
directly with the S3 client:

```python
from pathlib import Path

rclone.copy_file_s3(
    src=Path("/srv/out/model.bin"),
    dst="archive:models/model.bin",
)
```

The destination must include remote, bucket, and key. The configured remote
must have type `s3` or `b2`.

For a large remote-to-S3 copy that must resume after interruption, split the
source into explicit parts:

```python
from rclone_kit import PartInfo, SizeSuffix

source = "source:exports/full.tar"
destination = "archive:exports/full.tar"
source_size = rclone.size_file(source)
parts = PartInfo.split_parts(
    size=source_size,
    target_chunk_size=SizeSuffix("128M"),
)

rclone.copy_file_s3_resumable(
    src=source,
    dst=destination,
    part_infos=parts,
    upload_threads=8,
    merge_threads=4,
)
```

The operation stores resumable state and temporary objects beside the
destination using a `-parts` suffix. A later call with the same source,
destination, and part layout can resume completed work. Keep the source
immutable for the duration of the operation, choose a stable part size, and
do not purge the parts prefix while a retry may occur.

## Database inventory

Install `database` for SQLite or the appropriate database extra for the
server driver:

```python
rclone.save_to_db(
    src="archive:training-data",
    db_url="sqlite:///inventory.db",
    max_depth=-1,
    fast_list=False,
)
```

For PostgreSQL:

```python
import os

rclone.save_to_db(
    src="archive:training-data",
    db_url=os.environ["INVENTORY_DATABASE_URL"],
    max_depth=-1,
)
```

Pass the root-most path that should become one inventory table. The client
streams the listing in pages rather than loading it all in memory. Keep
database URLs out of logs because they may contain credentials.

## Remote control processes

Use rclone remote control only on a trusted network interface. Supply
authentication whenever the endpoint is reachable outside the process:

```python
with rclone.launch_server(
    addr="127.0.0.1:5572",
    user="worker",
    password=rc_password,
) as server_process:
    response = rclone.remote_control(
        addr="127.0.0.1:5572",
        user="worker",
        password=rc_password,
        capture=True,
        other_args=["core/version"],
    )
    if not response.ok:
        raise RuntimeError(response.stderr)
```

The `Process` context terminates the server and its child process tree.

## Logging and error handling

The library does not configure the root logger during import. Integrate it
with the application's logging policy:

```python
import logging

from rclone_kit import LogSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("rclone_kit").setLevel(logging.INFO)

LogSettings.rclone_verbose(True)
LogSettings.enable_upload_parts_logging(True)
```

Verbose command logging is useful during rollout but can be noisy. The
library redacts recognized credential arguments; the application remains
responsible for not logging config contents, database URLs, or arbitrary
`other_args`.

Handle the typed library errors at the boundary where retry or alert policy
is decided:

```python
import subprocess

from rclone_kit.exceptions import HttpFetchError, RcloneKitError
from rclone_kit.optional_dependency import MissingOptionalDependencyError

try:
    run_storage_job(rclone)
except MissingOptionalDependencyError:
    # Deployment packaging error: fail permanently and alert.
    raise
except HttpFetchError as error:
    # Network or remote HTTP error: apply the job's bounded retry policy.
    schedule_retry(error)
except RcloneKitError as error:
    mark_job_failed(error)
except subprocess.CalledProcessError as error:
    mark_job_failed(error)
```

`FileNotFoundError` is used for missing local or remote targets in several
filesystem and metadata operations. `ValueError` generally means invalid or
ambiguous input and should not be retried unchanged.

## Console scripts

The installed command-line adapters are useful for scheduled jobs and
operations tooling:

```bash
rclone-kit-listfiles --config /run/secrets/rclone.conf archive:dataset
rclone-kit-save-to-db --config /run/secrets/rclone.conf \
  --db sqlite:///inventory.db archive:dataset
rclone-kit-copylarge-s3 --config /run/secrets/rclone.conf \
  source:exports/full.tar archive:exports/full.tar
rclone-kit-install-bins
```

Run each command with `--help` before automation. Prefer the Python API when
the caller needs structured results, custom retry policy, or composition with
application transactions.

## Production checklist

Before rollout:

- pin the `rclone-kit` version and the required extras;
- deploy on a certified OS and architecture with Python 3.13 or newer;
- provide a writable per-user cache and enough VFS or temporary disk space;
- mount `rclone.conf` read-only from secret storage;
- validate required remotes and a representative read during startup;
- set explicit transfer, checker, partition-worker, and HTTP thread limits;
- use context managers for `FilesStream`, `RemoteFS`, `HttpServer`, `Mount`,
  and `Process`;
- make source data immutable during multipart and verification workflows;
- copy and verify before any purge or delete;
- bound retries at both rclone and job-queue layers to avoid retry storms;
- keep config text, database URLs, and credentials out of logs;
- monitor command duration, failure count, bytes transferred, cache space,
  and orphaned long-lived processes;
- exercise cloud, mount, and large-object smoke tests against the actual
  production backend before enabling destructive workflows.
