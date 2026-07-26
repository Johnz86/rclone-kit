"""`RcPath`: split rclone path syntax into the `fs`/`remote` pairs RC methods
expect, so listing, transfer, and delete code do not each reimplement path
splitting.

Two RC call shapes need different splits of the same input:

- whole-target calls (`operations/list`, `operations/stat`) want `fs` as the
  remote root (or local root) and `remote` as everything after it; and
- single-file calls (`operations/copyfile`'s `srcFs`/`srcRemote`) want `fs`
  as the containing directory and `remote` as just the final path component.

`RcPath.parse` produces the first shape; `as_parent_and_name` derives the
second from it. A third shape, `remote`/`parents`/`name` (a full directory
decomposition rather than a single split), is available via `parse_parts`
for callers building a tree from many paths (`group_files`); `split_remote_and_path`
is the free-standing `(remote_name, remainder)` split the other two shapes
share, for callers that need only that much.

Every bare local reference (no remote prefix) is absolutized against the
current process working directory as soon as it is parsed - see
`_resolve_local` for why this is required for correctness, not just style.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

_WINDOWS_DRIVE_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_DRIVE_PREFIX_LENGTH = 2


def is_windows_drive_prefix(path: str) -> bool:
    """True for `C:\\...` / `C:/...` / bare `C:`, which are local paths, not
    an rclone remote named `C`.
    """
    return (
        len(path) >= _DRIVE_PREFIX_LENGTH and path[0] in _WINDOWS_DRIVE_LETTERS and path[1] == ":"
    )


def _resolve_local(path: str) -> str:
    """Absolutize a bare local path before it crosses the RC boundary.

    rclone's `Fs` cache keys an instance by the literal string handed to
    it. A relative reference (`"."`, `"somedir"`, ...) only resolves
    against the *current* working directory the first time it is ever
    used against a given runtime; every later call with that same literal
    string reuses that first resolution, even after this process's cwd has
    since changed. This hazard only exists because `RcPath` addresses a
    shared, long-lived embedded runtime - a hypothetical short-lived
    process, torn down and relaunched per call, would have no such reuse
    to worry about - so resolving here, in Python, at the moment of the
    call, is the only way repeated relative-path calls stay correct.
    """
    if not path or not path.strip("/\\"):
        # Empty, or nothing but separator characters ("/", "\\", "//", ...):
        # there is no path component here at all, so leave it for
        # `as_parent_and_name`'s own "no path component to split" check
        # rather than letting `resolve()` turn it into a real (and
        # therefore no-longer-empty) drive-root path.
        return path
    return str(Path(path).resolve())


def split_remote_and_path(path: str) -> tuple[str, str]:
    """Split `path` into `(remote_name, remainder)`, the same shape
    `RcPath.parse` computes internally, without `_resolve_local`'s
    cwd-absolutizing side effect.

    For any caller that only needs to know "is there a remote here, and
    what's left over" - not the RC-ready `fs`/`remote` pair, which
    requires absolutizing a bare local reference before it crosses the RC
    boundary (see `_resolve_local`) - and is not about to reparse the
    same path through `RcPath.parse` for that purpose. Windows-drive-aware
    like `RcPath.parse`: `C:\\...` reports `remote_name=""` (a local path),
    never `remote_name="C"`.
    """
    if is_windows_drive_prefix(path):
        return "", path
    if path.startswith(":"):
        second_colon = path.find(":", 1)
        if second_colon == -1:
            return "", path
        return path[:second_colon], path[second_colon + 1 :].strip("/")
    remote_name, colon, rest = path.partition(":")
    if not colon:
        return "", path
    return remote_name, rest.strip("/")


@dataclass(frozen=True)
class RcPathParts:
    """One path fully decomposed into its remote name, parent directory
    segments, and final path component - the shape callers that build a
    directory tree from many paths need (`group_files`), as opposed to
    `RcPath`'s own `fs`/`remote` RC-call shape.
    """

    remote: str
    parents: list[str]
    name: str


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

        An inline remote (`:backend,param=value:path`) is split at its
        *second* colon, not its first - the first colon only opens the
        connection-string prefix, so naively splitting at the first colon
        would put nothing but a bare `":"` in `fs` and swallow the real
        backend/parameter prefix into `remote`.
        """
        if is_windows_drive_prefix(path):
            return cls(fs=path, remote="")
        if path.startswith(":"):
            second_colon = path.find(":", 1)
            if second_colon == -1:
                return cls(fs=path, remote="")
            return cls(fs=path[: second_colon + 1], remote=path[second_colon + 1 :].strip("/"))
        remote_name, colon, rest = path.partition(":")
        if not colon:
            return cls(fs=_resolve_local(path), remote="")
        return cls(fs=f"{remote_name}:", remote=rest.strip("/"))

    @classmethod
    def parse_parts(cls, path: str) -> RcPathParts:
        """Decompose `path` into `(remote, parents, name)`.

        A real remote's path is always forward-slash-delimited regardless
        of host OS (rclone remote paths are POSIX-shaped by protocol, not
        by host), so it splits on `PurePosixPath` - matching
        `group_files._get_prefix`'s existing precedent for the same
        reason. A Windows drive-letter local path (`C:\\...` or
        `C:/...`) splits on `PureWindowsPath` explicitly - not the host's
        native `Path` - so this is correct (and deterministically
        testable) on any host OS, not only when actually running on
        Windows: the drive-letter prefix is itself the unambiguous marker
        that this string is Windows-shaped, independent of the platform
        this process happens to run on. Any other local reference (a
        POSIX absolute path, or a bare relative path) splits on
        `PurePosixPath` too, the same as a real remote - there is no
        equivalent unambiguous marker for "this bare, colonless string is
        Windows-shaped" the way a drive letter is, so - like
        `_resolve_local` absolutizing every bare local reference the same
        way regardless of what it looks like - this is treated uniformly
        rather than guessed at.
        """
        if is_windows_drive_prefix(path):
            pure = PureWindowsPath(path)
            *parents, name = pure.parts[1:]
            return RcPathParts(remote=pure.drive.rstrip(":"), parents=parents, name=name)
        remote_name, rest = split_remote_and_path(path)
        posix_path = PurePosixPath(rest)
        posix_parts = posix_path.parts
        if posix_path.is_absolute():
            posix_parts = posix_parts[1:]
        if not posix_parts:
            return RcPathParts(remote=remote_name, parents=[], name=rest)
        *parents, name = posix_parts
        return RcPathParts(remote=remote_name, parents=parents, name=name)

    def as_parent_and_name(self) -> RcPath:
        """Return an `RcPath` split at the last path component instead: `fs`
        becomes the containing directory (or remote root) and `remote`
        becomes just the final name.

        Required for any single-target RC call (`operations/stat`,
        `operations/copyfile`'s `srcFs`/`srcRemote`, ...): `fs` must always
        be a navigable root - for a local target, the bare file's own full
        path is not a valid `fs` value, since rclone's local backend treats
        `fs` as a directory to open, not a file to stat directly.

        A local path with no directory separator at all (a bare relative
        basename like `"file.txt"`) splits to `fs=<absolute cwd>`,
        `remote="file.txt"` - the CLI accepts this bare form and resolves
        its parent as the current directory, so rejecting it here would
        make this helper stricter than the behavior it replaces. In
        practice `parse()` already absolutizes any bare local path before
        this method ever sees it, so this branch is a defensive fallback,
        not the normal path. A trailing separator is stripped before
        splitting, so a bare directory reference (`"foo/"`) still yields a
        name rather than an empty one.

        Raises `ValueError` if there is no path component left to split off
        at all: a bare remote root (`"remote:"`) or a bare local root with
        no path component (`"C:"`, `"/"`).
        """
        if self.remote:
            parent, sep, name = self.remote.rpartition("/")
            if not sep:
                return RcPath(fs=self.fs, remote=name)
            return RcPath(fs=f"{self.fs}{parent}", remote=name)
        if self.fs.endswith(":"):
            raise ValueError(f"{self!r} has no path component to split into parent and name")
        trimmed = self.fs.rstrip("\\/")
        if not trimmed:
            raise ValueError(f"{self!r} has no path component to split into parent and name")
        split_index = max(trimmed.rfind("\\"), trimmed.rfind("/"))
        if split_index == -1:
            return RcPath(fs=_resolve_local("."), remote=trimmed)
        name = trimmed[split_index + 1 :]
        if not name:
            raise ValueError(f"{self!r} has no path component to split into parent and name")
        return RcPath(fs=trimmed[: split_index + 1], remote=name)

    def __str__(self) -> str:
        return f"{self.fs}{self.remote}"
