"""`RcPath`: split rclone path syntax into the `fs`/`remote` pairs RC methods
expect, so listing, transfer, and delete code do not each reimplement path
splitting.

Two RC call shapes need different splits of the same input:

- whole-target calls (`operations/list`, `operations/stat`) want `fs` as the
  remote root (or local root) and `remote` as everything after it; and
- single-file calls (`operations/copyfile`'s `srcFs`/`srcRemote`) want `fs`
  as the containing directory and `remote` as just the final path component.

`RcPath.parse` produces the first shape; `as_parent_and_name` derives the
second from it.
"""

from __future__ import annotations

from dataclasses import dataclass

_WINDOWS_DRIVE_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_DRIVE_PREFIX_LENGTH = 2


def _is_windows_drive_prefix(path: str) -> bool:
    """True for `C:\\...` / `C:/...` / bare `C:`, which are local paths, not
    an rclone remote named `C`.
    """
    return (
        len(path) >= _DRIVE_PREFIX_LENGTH and path[0] in _WINDOWS_DRIVE_LETTERS and path[1] == ":"
    )


@dataclass(frozen=True)
class RcPath:
    """One rclone path, already split into the `fs` and `remote` RC expects.

    `fs` includes the trailing colon for a remote root (`"remote:"`) and is
    the bare local path for a local target; `remote` is `""` for a bare
    remote or local root.
    """

    fs: str
    remote: str

    @classmethod
    def parse(cls, path: str) -> RcPath:
        """Parse `path` into its remote root (or local path) and the
        remainder, treating a single-letter-plus-colon prefix as a Windows
        drive rather than a remote name.
        """
        if _is_windows_drive_prefix(path):
            return cls(fs=path, remote="")
        remote_name, colon, rest = path.partition(":")
        if not colon:
            return cls(fs=path, remote="")
        return cls(fs=f"{remote_name}:", remote=rest.strip("/"))

    def as_parent_and_name(self) -> RcPath:
        """Return an `RcPath` split at the last path component instead: `fs`
        becomes the containing directory (or remote root) and `remote`
        becomes just the final name, as `operations/copyfile`'s
        `srcFs`/`srcRemote` require.

        Raises `ValueError` if `remote` is empty (a bare root has no final
        component to split off).
        """
        if not self.remote:
            raise ValueError(f"{self!r} has no path component to split into parent and name")
        parent, sep, name = self.remote.rpartition("/")
        if not sep:
            return RcPath(fs=self.fs, remote=name)
        return RcPath(fs=f"{self.fs}{parent}", remote=name)

    def __str__(self) -> str:
        return f"{self.fs}{self.remote}"
