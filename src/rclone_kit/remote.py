from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rclone_kit.rclone_impl import RcloneImpl


class Remote:
    """Remote (root) directory."""

    def __init__(self, name: str, rclone: RcloneImpl) -> None:
        if ":" in name:
            raise ValueError("Remote name cannot contain ':'")

        self.name = name
        self.rclone: RcloneImpl = rclone

    def __str__(self) -> str:
        return f"{self.name}:"

    def __repr__(self) -> str:
        return f"Remote({self.name!r})"
