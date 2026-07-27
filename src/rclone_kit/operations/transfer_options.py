"""Typed transfer options and their RC `_config` encoding.

`TransferOptions` itself carries no field defaults beyond "let rclone use
its own configured value" (`None`): `copy()`'s tuned profile is a policy,
not a property of this generic type. The policy still lives here, next to
the type it is expressed in, as an ordinary `TransferOptions` value
(`COPY_TUNED_PROFILE`) that a call site must opt into explicitly through
`TransferOptions.with_defaults_from` - so it is declared exactly once and
`copy()`/`copy_files()` provably share it, while
`copy_dir()`/`copy_remote()`/`start_copy()`/`start_sync()`/`start_move()`
never inherit it and keep getting rclone's own configured values.

`COPY_TUNED_PROFILE_WITHOUT_RETRIES` is that same profile minus `retries`,
for `sync()`/`move()`: `_config.Retries` is read only by a command-level
retry loop, which upstream `sync/sync`/`sync/move` do not have.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace

_POSITIVE_INT_FIELDS = (
    "checkers",
    "transfers",
    "low_level_retries",
    "retries",
    "max_backlog",
)

# `--multi-thread-streams 0` is rclone's documented way to disable
# multi-thread transfers entirely (`fs/operations/multithread.go` bails out
# of `doMultiThreadCopy` on `MultiThreadStreams <= 1`), so zero is a
# meaningful setting here rather than an invalid one.
_NON_NEGATIVE_INT_FIELDS = ("multi_thread_streams",)

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
        self._validate_int_fields(_POSITIVE_INT_FIELDS, minimum=1, description="a positive integer")
        self._validate_int_fields(
            _NON_NEGATIVE_INT_FIELDS, minimum=0, description="a non-negative integer"
        )

    def _validate_int_fields(
        self, field_names: tuple[str, ...], *, minimum: int, description: str
    ) -> None:
        """Reject counts rclone cannot act on, before they reach `_config`.

        `bool` is rejected explicitly because it is an `int` subclass:
        `transfers=True` would otherwise silently mean `transfers=1`.
        """
        for field_name in field_names:
            value = getattr(self, field_name)
            if value is None:
                continue
            if isinstance(value, bool) or value < minimum:
                raise ValueError(f"{field_name} must be {description}, got {value!r}")

    def with_defaults_from(self, profile: TransferOptions) -> TransferOptions:
        """Return a copy with every unset (`None`) field taken from `profile`.

        The tuning policy is an argument, never a field default on this
        type: a caller that passes an explicit value keeps it untouched,
        and a call site that never names a profile at all still sends
        nothing, leaving rclone's own configured values in force.
        """
        overrides = {
            field.name: getattr(profile, field.name)
            for field in fields(self)
            if getattr(self, field.name) is None and getattr(profile, field.name) is not None
        }
        return replace(self, **overrides)


COPY_TUNED_PROFILE = TransferOptions(
    checkers=1000,
    transfers=32,
    low_level_retries=10,
    retries=3,
)
"""`copy()`'s aggressive tuned profile, shared verbatim by `copy_files()`.

Both run the retry-aware `rclonekit/copy` endpoint, which is the only one
that reads `_config.Retries`.
"""

COPY_TUNED_PROFILE_WITHOUT_RETRIES = replace(COPY_TUNED_PROFILE, retries=None)
"""`COPY_TUNED_PROFILE` for `sync()`/`move()`, whose upstream
`sync/sync`/`sync/move` endpoints have no command-level retry loop: a
`Retries` entry there would be config they can never act on."""


def encode_transfer_options_config(options: TransferOptions) -> dict[str, object]:
    """Encode `options` into the `_config` sub-object for an RC call.

    Only explicitly-set fields are included, so an RC call's defaults
    (rclone's own configured values) apply for anything the caller didn't
    specify. `multi_thread_streams` also sets `MultiThreadSet=True`: the
    CLI sets that companion flag whenever `--multi-thread-streams` is
    passed explicitly, and rclone's multi-thread decision logic reads it,
    not just the stream count.

    That companion flag is still correct for `multi_thread_streams=0`,
    even though zero means "disable multi-thread transfers": the CLI keys
    `MultiThreadSet` off the flag having been *changed*
    (`fs/config/configflags`), so an explicit `--multi-thread-streams 0`
    sets it too, and `doMultiThreadCopy` short-circuits on
    `MultiThreadStreams <= 1` before any `MultiThreadSet` check can make
    the pairing observable.
    """
    config: dict[str, object] = {}
    for field_name, config_key in _CONFIG_KEYS.items():
        value = getattr(options, field_name)
        if value is not None:
            config[config_key] = value
    if options.multi_thread_streams is not None:
        config["MultiThreadSet"] = True
    return config
