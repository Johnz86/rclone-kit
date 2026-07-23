"""`encode_fs_spec`: encode an rclone path/remote string into the value an
RC `srcFs`/`dstFs` parameter expects (CLI-to-C-ABI migration Wave D
design, section 9.3).

Most targets pass through as a plain string. A configured S3 or B2 remote
becomes rclone's documented RC config-object form instead, so
`no_check_bucket` can be set - the CLI's `copy`/`copy_dir`/`copy_remote`/
`copy_to` methods always pass `--s3-no-check-bucket`, and that is a
backend option with no `fs.ConfigInfo` field, so it cannot be expressed
through `_config` the way transfer tuning can.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rclone_kit.rc.paths import RcPath

if TYPE_CHECKING:
    from rclone_kit.config import Config

_S3_LIKE_BACKEND_TYPES = frozenset({"s3", "b2"})


def _configured_remote_type(config: Config, remote_name: str) -> str | None:
    try:
        section = config.parse().sections.get(remote_name)
    except Exception:
        return None
    if section is None:
        return None
    try:
        return section.type()
    except KeyError:
        return None


def encode_fs_spec(config: Config, spec: str) -> str | dict[str, object]:
    """Encode `spec` for use as an RC `srcFs`/`dstFs` value.

    Returns `spec` unchanged for an inline remote (not a named config
    section this can look up) or a remote whose configured backend is not
    S3/B2. For a configured S3 or B2 remote, returns `{"_name": <remote>,
    "_root": <path after the colon>, "no_check_bucket": "true"}` instead.

    For a local path, returns `target.fs` (via `str()`, to keep any
    `remote` component `RcPath.parse` split off) rather than `spec`
    unchanged: `RcPath.parse` absolutizes a bare local reference against
    the current working directory, and that absolutized form - not the
    original possibly-relative string - is what must reach the shared
    embedded runtime (see `rc/paths.py`'s `_resolve_local`).
    """
    target = RcPath.parse(spec)
    if not target.fs.endswith(":"):
        return str(target)
    if target.fs.startswith(":"):
        return spec
    remote_name = target.fs[:-1]
    if _configured_remote_type(config, remote_name) not in _S3_LIKE_BACKEND_TYPES:
        return spec
    return {
        "_name": remote_name,
        "_root": target.remote,
        "no_check_bucket": "true",
    }
