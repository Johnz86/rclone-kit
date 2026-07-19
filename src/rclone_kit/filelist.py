from dataclasses import dataclass

from rclone_kit.dir import Dir
from rclone_kit.dir_listing import DirListing
from rclone_kit.file import File
from rclone_kit.rpath import RPath


@dataclass
class FileList:
    """Remote file dataclass."""

    dirs: list[Dir]
    files: list[File]

    def _to_dir_list(self) -> list[RPath]:
        pathlist: list[RPath] = [d.path for d in self.dirs]
        pathlist.extend(f.path for f in self.files)
        return pathlist

    def __str__(self) -> str:
        pathlist: list[RPath] = self._to_dir_list()
        return str(DirListing(pathlist))

    def __repr__(self) -> str:
        pathlist: list[RPath] = self._to_dir_list()
        return repr(DirListing(pathlist))
