import hashlib
import json
import os
import warnings
from datetime import datetime

from rclone_kit.dir_listing import DirListing
from rclone_kit.rclone_impl import RcloneImpl
from rclone_kit.types import (
    PartInfo,
    SizeSuffix,
)


def _fetch_all_names(
    self: RcloneImpl,
    src: str,
) -> list[str]:
    dl: DirListing = self.ls(src)
    files = dl.files
    filenames: list[str] = [f.name for f in files]
    filtered: list[str] = [f for f in filenames if f.startswith("part.")]
    return filtered


def _get_info_json(self: RcloneImpl, src: str | None, src_info: str) -> dict:
    data: dict
    if src is None:
        try:
            text = self.read_text(src_info)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            raise FileNotFoundError(f"Could not load {src_info}: {error}") from error
        data = json.loads(text)
        return data

    src_stat = self.stat(src)

    now: datetime = datetime.now()
    new_data = {
        "new": True,
        "created": now.isoformat(),
        "src": src,
        "src_modtime": src_stat.mod_time(),
        "size": src_stat.size,
        "chunksize": None,
        "chunksize_int": None,
        "first_part": None,
        "last_part": None,
        "hash": None,
    }

    try:
        text = self.read_text(src_info)
    except KeyboardInterrupt:
        raise
    except Exception as error:
        warnings.warn(f"Failed to read {src_info}: {error}", stacklevel=2)
        return new_data

    try:
        data = json.loads(text)
        return data
    except Exception as e:
        warnings.warn(f"Failed to parse JSON: {e} at {src_info}", stacklevel=2)
        return new_data


def _save_info_json(self: RcloneImpl, src: str, data: dict) -> None:
    data = data.copy()
    data["new"] = False

    h = hashlib.md5()
    tmp = [
        data.get("src"),
        data.get("src_modtime"),
        data.get("size"),
        data.get("chunksize_int"),
    ]
    data_vals: list[str] = [str(v) for v in tmp]
    str_data = "".join(data_vals)
    h.update(str_data.encode("utf-8"))
    data["hash"] = h.hexdigest()
    json_str = json.dumps(data, indent=0)
    self.write_text(dst=src, text=json_str)


class InfoJson:
    def __init__(self, rclone: RcloneImpl, src: str | None, src_info: str) -> None:
        self.rclone = rclone
        self.src = src
        self.src_info = src_info
        self.data: dict = {}

    def load(self) -> bool:
        """Returns true if the file exist and is now loaded."""
        self.data = _get_info_json(self.rclone, self.src, self.src_info)
        return not self.data.get("new", False)

    def save(self) -> None:
        _save_info_json(self.rclone, self.src_info, self.data)

    def print(self) -> None:
        self.rclone.print(self.src_info)

    def fetch_all_finished(self) -> list[str]:
        parent_path = os.path.dirname(self.src_info)
        out = _fetch_all_names(self.rclone, parent_path)
        return out

    def fetch_all_finished_part_numbers(self) -> list[int]:
        names = self.fetch_all_finished()
        part_numbers = [int(name.split("_")[0].split(".")[1]) for name in names]
        return part_numbers

    @property
    def parts_dir(self) -> str:
        parts_dir = os.path.dirname(self.src_info)
        if parts_dir.endswith("/"):
            parts_dir = parts_dir[:-1]
        return parts_dir

    @property
    def dst(self) -> str:
        parts_dir = self.parts_dir
        assert parts_dir.endswith("-parts")
        out = parts_dir[:-6]
        return out

    @property
    def dst_name(self) -> str:
        return os.path.basename(self.dst)

    def compute_all_parts(self) -> list[PartInfo]:
        src_size = self.size
        chunk_size = self.chunksize
        assert isinstance(src_size, SizeSuffix)
        assert isinstance(chunk_size, SizeSuffix)
        first_part = self.data["first_part"]
        last_part = self.data["last_part"]
        full_part_infos: list[PartInfo] = PartInfo.split_parts(src_size, chunk_size)
        return full_part_infos[first_part : last_part + 1]

    def compute_all_part_numbers(self) -> list[int]:
        all_parts = self.compute_all_parts()
        return [p.part_number for p in all_parts]

    def fetch_remaining_part_numbers(self) -> list[int]:
        all_part_nums = self.compute_all_part_numbers()
        finished_part_nums: list[int] = self.fetch_all_finished_part_numbers()
        remaining_part_nums: list[int] = list(set(all_part_nums) - set(finished_part_nums))
        return sorted(remaining_part_nums)

    def fetch_is_done(self) -> bool:
        """Returns whether every part has been uploaded.

        Any failure computing the remaining parts (e.g. `first_part`/
        `last_part` not yet set) is treated as "not done".
        """
        try:
            remaining_part_nums = self.fetch_remaining_part_numbers()
        except Exception:
            return False
        return len(remaining_part_nums) == 0

    @property
    def new(self) -> bool:
        return self.data.get("new", False)

    @property
    def chunksize(self) -> SizeSuffix | None:
        chunksize_int: int | None = self.data.get("chunksize_int")
        if chunksize_int is None:
            return None
        return SizeSuffix(chunksize_int)

    @chunksize.setter
    def chunksize(self, value: SizeSuffix) -> None:
        self.data["chunksize"] = str(value)
        self.data["chunksize_int"] = value.as_int()

    @property
    def src_modtime(self) -> datetime:
        return datetime.fromisoformat(self.data["src_modtime"])

    @src_modtime.setter
    def src_modtime(self, value: datetime) -> None:
        self.data["src_modtime"] = value.isoformat()

    @property
    def size(self) -> SizeSuffix:
        return SizeSuffix(self.data["size"])

    @property
    def first_part(self) -> int | None:
        return self.data.get("first_part")

    @first_part.setter
    def first_part(self, value: int) -> None:
        self.data["first_part"] = value

    @property
    def last_part(self) -> int | None:
        return self.data.get("last_part")

    @last_part.setter
    def last_part(self, value: int) -> None:
        self.data["last_part"] = value

    @property
    def hash(self) -> str | None:
        return self.data.get("hash")

    def to_json_str(self) -> str:
        return json.dumps(self.data)

    def __repr__(self):
        return f"InfoJson({self.src}, {self.src_info}, {self.data})"

    def __str__(self):
        return self.to_json_str()
