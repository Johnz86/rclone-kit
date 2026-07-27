from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict, cast

from rclone_kit.remote import Remote

if TYPE_CHECKING:
    from rclone_kit.access import ListingAccess


type _RPathValue = tuple[Remote, str, str, int, str, str, bool]
"""`RPath`'s comparable/hashable projection: (remote, path, name, size,
mime_type, mod_time, is_dir)."""


class RcloneJsonEntry(TypedDict):
    """Shape of one `rclone lsjson` entry."""

    Path: str
    Name: str
    Size: int
    MimeType: str
    ModTime: str
    IsDir: bool


class RPath:
    """One entry of an `rclone lsjson` listing: a remote plus the metadata
    rclone reported for a path under it.

    Value semantics are hand-written rather than obtained from
    `@dataclass`, for two reasons a generated implementation cannot
    express here:

    - `__init__` normalises `path` (a trailing "/" is stripped) so that
      "a/b/" and "a/b" denote - and compare - as the same path. A frozen
      dataclass forbids that assignment outright, and an unfrozen one
      would need `__post_init__` plus `eq=True`/`unsafe_hash=True`, which
      is more machinery than the four lines below.
    - `rclone` is a mutable back-reference wired in after construction by
      `set_rclone`. It is the client used to *act* on the path, not part
      of what the path *is*, so it must stay out of both `__eq__` and
      `__hash__`; a dataclass would need `field(compare=False)` on it and
      would still generate an `__init__` that takes it.
    """

    def __init__(
        self,
        remote: Remote,
        path: str,
        name: str,
        size: int,
        mime_type: str,
        mod_time: str,
        is_dir: bool,
    ) -> None:
        if path.endswith("/"):
            path = path[:-1]
        self.remote = remote
        self.path = path
        self.name = name
        self.size = size
        self.mime_type = mime_type
        self.mod_time = mod_time
        self.is_dir = is_dir
        self.rclone: ListingAccess | None = None

    def _value(self) -> _RPathValue:
        """The fields that decide which listing entry this object is.

        Shared by `__eq__` and `__hash__` so the two can never drift apart.
        `rclone` is absent by design - see the class docstring.
        """
        return (
            self.remote,
            self.path,
            self.name,
            self.size,
            self.mime_type,
            self.mod_time,
            self.is_dir,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RPath):
            return False
        return self._value() == other._value()

    def __hash__(self) -> int:
        return hash(self._value())

    def mod_time_dt(self) -> datetime:
        """Return the modification time as a datetime object."""
        return datetime.fromisoformat(self.mod_time)

    def set_rclone(self, rclone: ListingAccess | None) -> None:
        """Set the rclone object."""
        self.rclone = rclone

    @staticmethod
    def from_dict(data: RcloneJsonEntry, remote: Remote, parent_path: str | None = None) -> RPath:
        """Create a File from a dictionary."""
        path = data["Path"]
        if parent_path is not None:
            path = f"{parent_path}/{path}"
        return RPath(
            remote,
            path,
            data["Name"],
            data["Size"],
            data["MimeType"],
            data["ModTime"],
            data["IsDir"],
        )

    @staticmethod
    def from_array(
        data: list[RcloneJsonEntry], remote: Remote, parent_path: str | None = None
    ) -> list[RPath]:
        """Create a File from a dictionary."""
        out: list[RPath] = []
        for d in data:
            file: RPath = RPath.from_dict(d, remote, parent_path)
            out.append(file)
        return out

    @staticmethod
    def from_json_str(json_str: str, remote: Remote, parent_path: str | None = None) -> list[RPath]:
        """Create a File from a JSON string."""
        json_obj = json.loads(json_str)
        if isinstance(json_obj, dict):
            return [RPath.from_dict(cast(RcloneJsonEntry, json_obj), remote, parent_path)]
        return RPath.from_array(cast(list[RcloneJsonEntry], json_obj), remote, parent_path)

    def to_json(self) -> RcloneJsonEntry:
        return {
            "Path": self.path,
            "Name": self.name,
            "Size": self.size,
            "MimeType": self.mime_type,
            "ModTime": self.mod_time,
            "IsDir": self.is_dir,
        }

    def __str__(self) -> str:
        if not self.remote.name:
            # No remote component at all (see `util.split_remote_name_and_
            # path`'s no-colon case) - omitting the colon here is required
            # for round-tripping: a leading/trailing ":" would make
            # `RcPath.parse` re-parse this as an (invalid) remote or inline
            # connection string instead of the original local path.
            return self.path
        return f"{self.remote.name}:{self.path}"

    def __repr__(self):
        data: dict[str, object] = {**self.to_json(), "Remote": self.remote.name}
        return json.dumps(data)
