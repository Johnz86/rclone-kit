from dataclasses import dataclass
from pathlib import PurePosixPath

from rclone_kit.rc.paths import RcPath, RcPathParts, is_windows_drive_prefix

_MAX_CHILDREN_FOR_INDIVIDUAL_MERGE = 2


@dataclass
class PrefixResult:
    prefix: str
    files: list[str]


def parse_file(file_path: str) -> RcPathParts:
    """Parse file path into parts.

    A colonless `file_path` (a local filesystem path, not `remote:path`)
    parses with `remote=""` rather than raising - `delete_files_embedded`
    calls this on real local paths too (via `RemoteFS.remove()`), and on
    Linux those never contain a colon at all. A Windows path also parses
    with `remote=""` and its drive letter (`C:\\...`) folded into
    `parents`/`name` via `RcPath.parse_parts` - not the pre-existing
    accidental `remote="C"` split this used to do by hand, which never
    split on `\\` at all and so collapsed a Windows path's whole
    directory structure into one opaque `name` (see `_colonify` for how
    `group_files` reassembles a Windows drive's parent path once split
    this way).
    """
    assert not file_path.endswith("/"), "This looks like a directory path"
    return RcPath.parse_parts(file_path)


def _to_string(parts: RcPathParts, include_remote: bool, include_bucket: bool) -> str:
    """Convert `parts` back to a string, may throw for not include_bucket=False."""
    parents = list(parts.parents)
    if not include_bucket:
        parents.pop(0)
    path = "/".join(parents)
    if path:
        path += "/"
    path += parts.name
    if include_remote:
        return f"{parts.remote}{path}"
    return path


class TreeNode:
    def __init__(
        self,
        name: str,
        child_nodes: dict[str, "TreeNode"] | None = None,
        files: list[str] | None = None,
        parent: "TreeNode | None" = None,
    ):
        self.name = name
        self.child_nodes = child_nodes or {}
        self.files = files or []
        self.count = 0
        self.parent = parent

    def add_count_bubble_up(self):
        self.count += 1
        if self.parent:
            self.parent.add_count_bubble_up()

    def get_path(self) -> str:
        paths_reversed: list[str] = [self.name]
        node: TreeNode | None = self
        assert node is not None
        while True:
            node = node.parent
            if node is None:
                break
            paths_reversed.append(node.name)
        return "/".join(reversed(paths_reversed))

    def get_child_subpaths(self, parent_path: str | None = None) -> list[str]:
        paths: list[str] = []
        for child in self.child_nodes.values():
            child_paths = child.get_child_subpaths(parent_path=child.name)
            paths.extend(child_paths)
        for file in self.files:
            full_path = f"{parent_path}/{file}" if parent_path else file
            paths.append(full_path)
        return paths

    def __repr__(self, indent: int = 0) -> str:

        leftpad = " " * indent
        msg = f"{leftpad}{self.name}: {self.count}"
        if self.child_nodes:
            msg += "\n"
            for child in self.child_nodes.values():
                msg += child.__repr__(indent + 2)
        return msg


def _flatten_into(node: TreeNode, parent_path: str, out: dict[str, list[str]]) -> None:
    """Append every file under `node` (recursively) to `out[parent_path]`."""
    filelist = out.setdefault(parent_path, [])
    filelist.extend(node.get_child_subpaths())


def _merge(node: TreeNode, parent_path: str, out: dict[str, list[str]]) -> None:
    parent_path = parent_path + "/" + node.name
    if not node.child_nodes and not node.files:
        return
    if node.files:
        _flatten_into(node, parent_path, out)
        return

    n_child_nodes = len(node.child_nodes)

    if n_child_nodes <= _MAX_CHILDREN_FOR_INDIVIDUAL_MERGE:
        for child in node.child_nodes.values():
            _merge(child, parent_path, out)
        return

    _flatten_into(node, parent_path, out)


def _make_tree(files: list[str]) -> dict[str, TreeNode]:
    tree: dict[str, TreeNode] = {}
    for file in files:
        parts = parse_file(file)
        remote = parts.remote
        node: TreeNode = tree.setdefault(remote, TreeNode(remote))
        if parts.parents:
            for parent in parts.parents:
                is_last = parent == parts.parents[-1]
                node = node.child_nodes.setdefault(parent, TreeNode(parent, parent=node))
                if is_last:
                    node.files.append(parts.name)
                    node.add_count_bubble_up()
        else:
            node.files.append(parts.name)
            node.add_count_bubble_up()

    return tree


def _colonify(path: str) -> str:
    """Turn one `/`-stripped tree path into its final `group_files()` key.

    A doubled leading "/" means the top-level node's name was "" - a
    local, colonless path from `parse_file`, not a real remote name - so
    `path` is already a plain absolute local path with no remote segment
    to colon-ify; return it unchanged. Otherwise the first "/" marks the
    end of the remote name and becomes ":" - except when that remote name
    is a Windows drive letter (`parse_file` folds `C:\\...` into
    `remote="C"`, `parents=[...]`), where the "/" must be *kept* rather
    than consumed: "C:/Users" is an absolute path, but "C:Users" is a
    valid, entirely different Windows path meaning "relative to the
    current directory on the C: drive."
    """
    if path.startswith("/"):
        return path
    head, sep, rest = path.partition("/")
    if not sep:
        return path
    if is_windows_drive_prefix(f"{head}:"):
        return f"{head}:/{rest}"
    return f"{head}:{rest}"


def _fixup_rclone_paths(outpaths: dict[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path, files in outpaths.items():
        assert path.startswith("/"), "Path should start with /"
        out[_colonify(path[1:])] = files
    return out


def group_files(files: list[str], fully_qualified: bool = True) -> dict[str, list[str]]:
    """split between filename and parent directory path"""
    if fully_qualified is False:
        for i, file in enumerate(files):
            prefixed_file = "root:" + file
            files[i] = prefixed_file
    tree: dict[str, TreeNode] = _make_tree(files)
    outpaths: dict[str, list[str]] = {}
    for node in tree.values():
        _merge(node, "", outpaths)
    tmp: dict[str, list[str]] = _fixup_rclone_paths(outpaths=outpaths)
    out: dict[str, list[str]] = {}
    if fully_qualified is False:
        for path, path_files in tmp.items():
            trimmed_path = path
            if trimmed_path.startswith("root"):
                trimmed_path = trimmed_path.replace("root", "")
                if trimmed_path.startswith(":"):
                    trimmed_path = trimmed_path[1:]
            out[trimmed_path] = [file.replace("/root/", "") for file in path_files]
    else:
        out = tmp
    return out


def group_under_remote_bucket(
    files: list[str], fully_qualified: bool = True
) -> dict[str, list[str]]:
    """split between filename and bucket"""
    assert fully_qualified is True, "Not implemented for fully_qualified=False"
    out: dict[str, list[str]] = {}
    for file in files:
        parsed = parse_file(file)
        remote = f"{parsed.remote}:"
        parts = parsed.parents
        bucket = parts[0]
        remote_bucket = f"{remote}{bucket}"
        file_list = out.setdefault(remote_bucket, [])
        file_list.append(_to_string(parsed, include_remote=False, include_bucket=False))
    return out


def _get_prefix(path: str) -> tuple[str, str] | None:
    """Split `path` on its first `/`.

    Uses `PurePosixPath`, not `Path`, because `path` is always a
    forward-slash-delimited rclone remote path, never a local filesystem
    path. `Path` resolves to `WindowsPath` on Windows, which treats a
    literal `\\` inside a path segment (a valid character in many remote
    object keys) as a separator, silently splitting one filename into two
    path components - a bug that only reproduces on Windows.
    """
    path_path = PurePosixPath(path)
    parts = path_path.parts
    if len(parts) == 1:
        return None
    return parts[0], "/".join(parts[1:])


def _common_prefix(prefix: str, files: list[str]) -> PrefixResult:
    if not files:
        return PrefixResult(prefix=prefix, files=[])
    tmp: list[str] = list(files)
    while True:
        if not tmp:
            break
        prefix_set: set[str | None] = set()
        for file in tmp:
            pair = _get_prefix(file)
            if pair is None:
                break
            _prefix, _ = pair
            prefix_set.add(_prefix)
        if len(prefix_set) > 1 or len(prefix_set) == 0:
            break
        next_prefix: str | None = prefix_set.pop()
        if next_prefix is None:
            break
        prefix += f"/{next_prefix}"
        new_tmp: list[str] = []
        for file in tmp:
            pair = _get_prefix(file)
            assert pair is not None
            _, path = pair
            new_tmp.append(path)
        tmp = new_tmp
    return PrefixResult(prefix=prefix, files=tmp)


def group_under_one_prefix(prefix: str, files: list[str]) -> tuple[str, list[str]]:
    """Group files under one prefix."""
    if not files:
        return prefix, []
    result = _common_prefix(prefix, files)
    return result.prefix.replace(":/", ":"), result.files


__all__ = ["group_files", "group_under_one_prefix", "group_under_remote_bucket"]
