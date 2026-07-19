# rclone-kit

[![CI](https://github.com/Johnz86/rclone-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/Johnz86/rclone-kit/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/rclone-kit.svg)](https://pypi.org/project/rclone-kit/)

A fast, Pythonic API around [rclone](https://rclone.org/), built for moving
large volumes of data — originally out of necessity for shipping AI training
data around quickly. Aggressive default transfer settings mean this beats a
stock rclone invocation out of the box.

The published wheel bundles a pinned, checksum-verified rclone executable
for Windows and Linux (amd64) — no separate rclone install or PATH setup
required.

## Install

```bash
pip install rclone-kit
```

Optional extras pull in only what you need:

```bash
pip install "rclone-kit[s3]"
pip install "rclone-kit[database]"
pip install "rclone-kit[postgres]"
pip install "rclone-kit[full]"
```

The extras install S3 helpers, database export support, PostgreSQL support,
or the complete optional feature set, respectively.

Requires Python 3.13+. Supported platforms: Windows amd64 and Linux amd64
(`manylinux2014_x86_64`).

## Quick start

```python
from rclone_kit import Rclone, Config

rclone = Rclone(Config("""
[dst]
type = s3
account = ...
key = ...
"""))

listing = rclone.ls("dst:my-bucket/data", glob="*.png")
for file in listing.files:
    print(file.path)

rclone.copy("local:/data/incoming", "dst:my-bucket/data")
```

Already have an `rclone.conf` on disk? Point `RCLONE_CONFIG` at it and pass
`None` instead of a `Config` — `Rclone(None)` picks it up automatically.

## Highlights

- **Aggressive defaults** for copy/sync operations, tuned for large transfers.
- **Fast streaming diff** between source and destination — find missing
  files without materializing the whole tree in memory.
- **Directory walking**, breadth-first or depth-first.
- **Resumable multi-part S3 uploads** that survive interrupted transfers.
- **Database export**: dump a remote's file listing straight into
  SQLite/Postgres/MySQL, one repo path per table.
- **Byte-range HTTP serving** for slicing chunks out of very large remote
  files.
- **Scoped mount and HTTP-server objects** with guaranteed cleanup, and
  platform-specific mount setup/teardown handled for you.
- **`FSPath`**: a `pathlib.Path`-like virtual filesystem over any remote —
  get one via `rclone.cwd("dst:path/to")`.

## Console scripts

Installed alongside the library:

| Command | Purpose |
|---|---|
| `rclone-kit-listfiles` | Walk a remote path and print every file found. |
| `rclone-kit-copylarge-s3` | Chunked, resumable large-file copy to S3. |
| `rclone-kit-save-to-db` | Dump a remote's listing into a SQL database. |
| `rclone-kit-install-bins` | Install the pinned, verified rclone executable for the current platform. |

Run any of them with `--help` for full usage.

## Configuration

- `RCLONE_CONFIG` — path to an `rclone.conf` file. If unset, `rclone config
  paths` is used to discover the active config.

## Contributing

This project uses [uv](https://docs.astral.sh/uv/) as the only project,
environment, dependency, build, and publishing frontend — no OS-specific
wrapper scripts.

```bash
git clone https://github.com/Johnz86/rclone-kit
cd rclone-kit
uv python install
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run pyright _build_backend.py src tests scripts
uv run pytest tests/unit
uv run pytest tests/integration
```

Run `uv run ruff format .` and `uv run ruff check --fix .` before committing.

See `docs/release_process.md` for how release builds and publishing work.
