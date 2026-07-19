import random
import subprocess
import warnings
from collections.abc import Generator
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from tempfile import TemporaryDirectory

from rclone_kit.convert import convert_to_str
from rclone_kit.diff import DiffItem, DiffOption, diff_stream_from_running_process
from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.file import File
from rclone_kit.rclone_impl import FLAG_CHECKERS, FLAG_FAST_LIST, FLAG_FILES_FROM, RcloneImpl
from rclone_kit.remote import Remote
from rclone_kit.rpath import RPath
from rclone_kit.types import ListingOption, Order, SizeResult, SizeSuffix
from rclone_kit.util import get_check, get_verbose, to_path


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


def fetch_size_file(self: RcloneImpl, src: str) -> SizeSuffix:
    """Get the size of a file or directory.

    Raises FileNotFoundError if no file matches `src`, or ValueError
    if more than one file matches.
    """
    dirlist: DirListing = self.ls(src, listing_option=ListingOption.FILES_ONLY, max_depth=0)
    if len(dirlist.files) == 0:
        raise FileNotFoundError(f"File not found: {src}")
    if len(dirlist.files) > 1:
        raise ValueError(f"More than one file found: {src}")
    return SizeSuffix(dirlist.files[0].size)


def fetch_size_files(
    self: RcloneImpl,
    src: str,
    files: list[str],
    fast_list: bool = False,
    other_args: list[str] | None = None,
    check: bool | None = False,
    verbose: bool | None = None,
) -> SizeResult:
    """Get the size of a list of files. Example of files items: "remote:bucket/to/file"."""
    verbose = get_verbose(verbose)
    check = get_check(check)
    if not files:
        return SizeResult(prefix=src, total_size=0, file_sizes={})
    if len(files) < 2:
        full_path = f"{src}/{files[0]}"
        tmp = self.size_file(full_path)
        return SizeResult(prefix=src, total_size=tmp.as_int(), file_sizes={files[0]: tmp.as_int()})
    if fast_list or (other_args and FLAG_FAST_LIST in other_args):
        warnings.warn(
            "It's not recommended to use --fast-list with size_files as this will perform poorly on large repositories since the entire repository has to be scanned.",
            stacklevel=2,
        )
    files = list(files)
    all_files: list[File] = []

    cmd = ["lsjson", src, "--files-only", "-R"]
    with TemporaryDirectory() as tmpdir:
        include_files_txt = Path(tmpdir) / "include_files.txt"
        include_files_txt.write_text("\n".join(files), encoding="utf-8")
        cmd += [FLAG_FILES_FROM, str(include_files_txt)]
        if fast_list:
            cmd.append(FLAG_FAST_LIST)
        if other_args:
            cmd += other_args
        cp = self._run(cmd, check=check)

        if cp.returncode != 0:
            if check:
                raise ValueError(f"Error getting file sizes: {cp.stderr}")
            else:
                warnings.warn(f"Error getting file sizes: {cp.stderr}", stacklevel=2)
        stdout = cp.stdout
        pieces = src.split(":", 1)
        remote_name = pieces[0]
        parent_path: str | None
        parent_path = pieces[1] if len(pieces) > 1 else None
        remote = Remote(name=remote_name, rclone=self)
        paths: list[RPath] = RPath.from_json_str(stdout, remote, parent_path=parent_path)

        all_files += [File(p) for p in paths]
    file_sizes: dict[str, int] = {}
    f: File
    for f in all_files:
        p = f.to_string(include_remote=True)
        if p in file_sizes:
            warnings.warn(f"Duplicate file found: {p}", stacklevel=2)
            continue
        size = f.size
        if size == 0:
            warnings.warn(f"File size is 0: {p}", stacklevel=2)
        file_sizes[p] = f.size
    total_size = sum(file_sizes.values())
    file_sizes_path_corrected: dict[str, int] = {}
    for path, size in file_sizes.items():
        prefix = src.rstrip("/") + "/"
        if not path.startswith(prefix):
            raise ValueError(f"Listed path {path!r} is outside source {src!r}")
        file_sizes_path_corrected[path.removeprefix(prefix)] = size
    out: SizeResult = SizeResult(
        prefix=src, total_size=total_size, file_sizes=file_sizes_path_corrected
    )
    return out


def stream_diff(
    self: RcloneImpl,
    src: str,
    dst: str,
    min_size: str | None = None,
    max_size: str | None = None,
    diff_option: DiffOption = DiffOption.COMBINED,
    fast_list: bool = True,
    size_only: bool | None = None,
    checkers: int | None = None,
    other_args: list[str] | None = None,
) -> Generator[DiffItem]:
    """Be extra careful with the src and dst values. If you are off by one
    parent directory, you will get a huge amount of false diffs."""
    other_args = other_args or []
    if checkers is None or checkers < 1:
        checkers = 1000
    cmd = [
        "check",
        src,
        dst,
        FLAG_CHECKERS,
        str(checkers),
        "--log-level",
        "INFO",
        f"--{diff_option.value}",
        "-",
    ]
    if size_only is None:
        size_only = diff_option in [
            DiffOption.MISSING_ON_DST,
            DiffOption.MISSING_ON_SRC,
        ]
    if size_only:
        cmd += ["--size-only"]
    if fast_list:
        cmd += [FLAG_FAST_LIST]
    if min_size:
        cmd += ["--min-size", min_size]
    if max_size:
        cmd += ["--max-size", max_size]
    if diff_option == DiffOption.MISSING_ON_DST:
        cmd += ["--one-way"]
    if other_args:
        cmd += other_args
    proc = self._launch_process(cmd, capture=True)
    item: DiffItem
    for item in diff_stream_from_running_process(
        running_process=proc, src_slug=src, dst_slug=dst, diff_option=diff_option
    ):
        if item is None:
            break
        yield item
