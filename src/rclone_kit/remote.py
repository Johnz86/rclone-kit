from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rclone_kit.access import ListingAccess


class Remote:
    """Remote (root) directory.

    A value object keyed on `name`: within one rclone configuration the
    name *is* the remote. `rclone` is only the client used to act on it,
    so it is deliberately excluded from `__eq__`/`__hash__` - otherwise
    two listings of the same remote taken through two clients would
    compare unequal and silently defeat every `RPath` set or dict that
    transitively hashes a `Remote`.
    """

    def __init__(self, name: str, rclone: ListingAccess) -> None:
        if ":" in name:
            raise ValueError("Remote name cannot contain ':'")

        self.name = name
        self.rclone: ListingAccess = rclone

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Remote):
            return False
        return self.name == other.name

    def __hash__(self) -> int:
        return hash(self.name)

    def __str__(self) -> str:
        return f"{self.name}:"

    def __repr__(self) -> str:
        return f"Remote({self.name!r})"
