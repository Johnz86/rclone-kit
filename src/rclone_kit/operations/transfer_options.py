"""Typed transfer options and their RC `_config` encoding.

`TransferOptions` itself carries no defaults beyond "let rclone use its
own configured value" (`None`): `copy()`'s tuned profile (checkers 1000,
transfers 32, low-level retries 10, retries 3) is a policy that belongs at
its own call site, not baked into this generic type -
`copy_dir()`/`copy_remote()` must not inherit it.
"""

from __future__ import annotations

from dataclasses import dataclass

_POSITIVE_INT_FIELDS = (
    "checkers",
    "transfers",
    "low_level_retries",
    "retries",
    "multi_thread_streams",
    "max_backlog",
)

_CONFIG_KEYS = {
    "checkers": "Checkers",
    "transfers": "Transfers",
    "low_level_retries": "LowLevelRetries",
    "retries": "Retries",
    "multi_thread_streams": "MultiThreadStreams",
    "retries_sleep": "RetriesInterval",
    "timeout": "Timeout",
    "max_backlog": "MaxBacklog",
    "metadata": "Metadata",
}


@dataclass(frozen=True)
class TransferOptions:
    """Typed transfer tuning for an embedded copy/sync-shaped RC call.

    `create_empty_src_dirs` is not part of `_config`: it is its own RC
    method parameter (`createEmptySrcDirs`) on `sync/copy`/`rclonekit/copy`,
    encoded separately by the caller, not by `encode_transfer_options_config`.

    `retries_sleep`/`timeout` take the same duration-string shape as their
    CLI flag values (e.g. `"10s"`); `fs.ConfigInfo`'s `RetriesInterval`/
    `Timeout` fields parse that string identically over RC.
    """

    checkers: int | None = None
    transfers: int | None = None
    low_level_retries: int | None = None
    retries: int | None = None
    multi_thread_streams: int | None = None
    retries_sleep: str | None = None
    timeout: str | None = None
    max_backlog: int | None = None
    metadata: bool | None = None
    create_empty_src_dirs: bool = False

    def __post_init__(self) -> None:
        for field_name in _POSITIVE_INT_FIELDS:
            value = getattr(self, field_name)
            if value is None:
                continue
            if isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer, got {value!r}")


def encode_transfer_options_config(options: TransferOptions) -> dict[str, object]:
    """Encode `options` into the `_config` sub-object for an RC call.

    Only explicitly-set fields are included, so an RC call's defaults
    (rclone's own configured values) apply for anything the caller didn't
    specify. `multi_thread_streams` also sets `MultiThreadSet=True`: the
    CLI sets that companion flag whenever `--multi-thread-streams` is
    passed explicitly, and rclone's multi-thread decision logic reads it,
    not just the stream count.
    """
    config: dict[str, object] = {}
    for field_name, config_key in _CONFIG_KEYS.items():
        value = getattr(options, field_name)
        if value is not None:
            config[config_key] = value
    if options.multi_thread_streams is not None:
        config["MultiThreadSet"] = True
    return config
