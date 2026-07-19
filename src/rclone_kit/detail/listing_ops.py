import random
import subprocess
from datetime import datetime
from fnmatch import fnmatch

from rclone_kit.convert import convert_to_str
from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.file import File
from rclone_kit.rclone_impl import RcloneImpl
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath
from rclone_kit.types import ListingOption, Order
from rclone_kit.util import to_path


def fetch_ls(
    self: RcloneImpl,
    src: Dir | Remote | str | None = None,
    max_depth: int | None = None,
    glob: str | None = None,
    order: Order = Order.NORMAL,
    listing_option: ListingOption = ListingOption.ALL,
) -> DirListing:
    """List files in the given path."""
    if src is None:
        list_remotes: list[Remote] = self.listremotes()
        dirs: list[Dir] = [Dir(remote) for remote in list_remotes]
        for d in dirs:
            d.path.path = ""
        rpaths = [d.path for d in dirs]
        return DirListing(rpaths)

    if isinstance(src, str):
        src = Dir(to_path(src, self))

    cmd = ["lsjson"]
    if max_depth is not None:
        if max_depth < 0:
            cmd.append("--recursive")
        if max_depth > 0:
            cmd.append("--max-depth")
            cmd.append(str(max_depth))
    if listing_option != ListingOption.ALL:
        cmd.append(f"--{listing_option.value}")

    cmd.append(str(src))
    remote = src.remote if isinstance(src, Dir) else src
    assert isinstance(remote, Remote)

    cp = self._run(cmd, check=True)
    text = cp.stdout
    parent_path: str | None = None
    if isinstance(src, Dir):
        parent_path = src.path.path
    paths: list[RPath] = RPath.from_json_str(text, remote, parent_path=parent_path)

    for o in paths:
        o.set_rclone(self)

    if glob is not None:
        paths = [p for p in paths if fnmatch(p.path, glob)]

    if order == Order.REVERSE:
        paths.reverse()
    elif order == Order.RANDOM:
        random.shuffle(paths)
    return DirListing(paths)


def print_contents(self: RcloneImpl, src: str) -> None:
    """Print the contents of a file."""
    print(self.read_text(src))


def fetch_stat(self: RcloneImpl, src: str) -> File:
    """Get the status of a file or directory.

    Raises FileNotFoundError if `src` does not exist.
    """
    dirlist: DirListing = self.ls(src)
    if len(dirlist.files) == 0:
        raise FileNotFoundError(f"File not found: {src}")
    return dirlist.files[0]


def fetch_listremotes(self: RcloneImpl) -> list[Remote]:
    cmd = ["listremotes"]
    cp = self._run(cmd)
    text: str = cp.stdout
    tmp = text.splitlines()
    tmp = [t.strip() for t in tmp]

    tmp = [t.replace(":", "") for t in tmp]
    out = [Remote(name=t, rclone=self) for t in tmp]
    return out


def check_exists(self: RcloneImpl, src: Dir | Remote | str | File) -> bool:
    """Check if a file or directory exists."""
    arg: str = convert_to_str(src)
    assert isinstance(arg, str)
    try:
        dir_listing = self.ls(arg)

        return len(dir_listing.dirs) > 0 or len(dir_listing.files) > 0
    except subprocess.CalledProcessError:
        return False


def check_is_synced(self: RcloneImpl, src: str | Dir, dst: str | Dir) -> bool:
    """Check if two directories are in sync."""
    src = convert_to_str(src)
    dst = convert_to_str(dst)
    cmd_list: list[str] = ["check", str(src), str(dst)]
    try:
        self._run(cmd_list, check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def fetch_modtime(self: RcloneImpl, src: str) -> str:
    """Get the modification time of a file or directory."""
    return self.stat(src).mod_time()


def fetch_modtime_dt(self: RcloneImpl, src: str) -> datetime:
    """Get the modification time of a file or directory."""
    return self.stat(src).mod_time_dt()
