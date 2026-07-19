import os
import re
import time
import warnings
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Self


class ModTimeStrategy(Enum):
    USE_SERVER_MODTIME = "use-server-modtime"
    NO_MODTIME = "no-modtime"


class ListingOption(Enum):
    DIRS_ONLY = "dirs-only"
    FILES_ONLY = "files-only"
    ALL = "all"


class Order(Enum):
    NORMAL = "normal"
    REVERSE = "reverse"
    RANDOM = "random"


@dataclass
class S3PathInfo:
    remote: str
    bucket: str
    key: str

    @staticmethod
    def from_str(src: str) -> "S3PathInfo":
        from rclone_kit.util import split_s3_path

        return split_s3_path(src)


@dataclass
class SizeResult:
    """Size result dataclass."""

    prefix: str
    total_size: int
    file_sizes: dict[str, int]


def _to_size_suffix(size: int) -> str:
    def _convert(size: int) -> tuple[float, str]:
        val: float
        unit: str
        if size < 1024:
            val = size
            unit = "B"
        elif size < 1024**2:
            val = size / 1024
            unit = "K"
        elif size < 1024**3:
            val = size / (1024**2)
            unit = "M"
        elif size < 1024**4:
            val = size / (1024**3)
            unit = "G"
        elif size < 1024**5:
            val = size / (1024**4)
            unit = "T"
        elif size < 1024**6:
            val = size / (1024**5)
            unit = "P"
        else:
            raise ValueError(f"Invalid size: {size}")

        return val, unit

    def _fmt(_val: float, _unit: str) -> str:
        first_str = str(int(_val)) if float(_val).is_integer() else f"{_val:.1f}"
        return first_str + _unit

    val, unit = _convert(size)
    out = _fmt(val, unit)
    # Now round trip the value to fix floating point issues via rounding.
    int_val = _from_size_suffix(out)
    val, unit = _convert(int_val)
    out = _fmt(val, unit)
    return out


# Update regex to allow decimals (e.g., 16.5MB)
_PATTERN_SIZE_SUFFIX = re.compile(r"^(\d+(?:\.\d+)?)([A-Za-z]+)$")


def _parse_elements(value: str) -> tuple[str, str] | None:
    match = _PATTERN_SIZE_SUFFIX.match(value)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _from_size_suffix(size: str) -> int:
    if size == "0":
        return 0
    pair = _parse_elements(size)
    if pair is None:
        raise ValueError(f"Invalid size suffix: {size}")
    num_str, suffix = pair
    n = float(num_str)
    # Determine the unit from the first letter (e.g., "M" from "MB")
    unit = suffix[0].upper()
    if unit == "B":
        return int(n)
    if unit == "K":
        return int(n * 1024)
    if unit == "M":
        return int(n * 1024 * 1024)
    if unit == "G":
        return int(n * 1024 * 1024 * 1024)
    if unit == "T":
        return int(n * 1024**4)
    if unit == "P":
        return int(n * 1024**5)
    raise ValueError(f"Invalid size suffix: {suffix}")


@dataclass(frozen=True, slots=True, init=False)
class SizeSuffix:
    _size: int

    def __init__(self, size: "int | str | SizeSuffix"):
        parsed_size: int
        if isinstance(size, SizeSuffix):
            parsed_size = size._size
        elif isinstance(size, int):
            parsed_size = size
        elif isinstance(size, str):
            parsed_size = _from_size_suffix(size)
        elif isinstance(size, float):
            parsed_size = int(size)
        else:
            raise ValueError(f"Invalid type for size: {type(size)}")
        object.__setattr__(self, "_size", parsed_size)

    def as_int(self) -> int:
        return self._size

    def as_str(self) -> str:
        return _to_size_suffix(self._size)

    def __repr__(self) -> str:
        return self.as_str()

    def __str__(self) -> str:
        return self.as_str()

    @staticmethod
    def _to_size(size: "int | SizeSuffix") -> int:
        if isinstance(size, int):
            return size
        elif isinstance(size, SizeSuffix):
            return size._size
        else:
            raise ValueError(f"Invalid type for size: {type(size)}")

    def __mul__(self, other: "int | SizeSuffix") -> "SizeSuffix":
        other_int = SizeSuffix(other)
        return SizeSuffix(self._size * other_int._size)

    # multiply when int is on the left
    def __rmul__(self, other: "int | SizeSuffix") -> "SizeSuffix":
        return self.__mul__(other)

    def __add__(self, other: "int | SizeSuffix") -> "SizeSuffix":
        other_int = SizeSuffix(other)
        return SizeSuffix(self._size + other_int._size)

    def __radd__(self, other: "int | SizeSuffix") -> "SizeSuffix":
        return self.__add__(other)

    def __sub__(self, other: "int | SizeSuffix") -> "SizeSuffix":
        other_int = SizeSuffix(other)
        return SizeSuffix(self._size - other_int._size)

    def __rsub__(self, other: "int | SizeSuffix") -> "SizeSuffix":
        other_int = SizeSuffix(other)
        return SizeSuffix(other_int._size - self._size)

    def __truediv__(self, other: "int | SizeSuffix") -> "SizeSuffix":
        other_int = SizeSuffix(other)
        if other_int._size == 0:
            raise ZeroDivisionError("Division by zero is undefined")
        # Use floor division to maintain integer arithmetic.
        return SizeSuffix(self._size // other_int._size)

    def __rtruediv__(self, other: "int | SizeSuffix") -> "SizeSuffix":
        other_int = SizeSuffix(other)
        if self._size == 0:
            raise ZeroDivisionError("Division by zero is undefined")
        # Use floor division to maintain integer arithmetic.
        return SizeSuffix(other_int._size // self._size)

    # support / division
    def __floordiv__(self, other: "int | SizeSuffix") -> "SizeSuffix":
        other_int = SizeSuffix(other)
        if other_int._size == 0:
            raise ZeroDivisionError("Division by zero is undefined")
        # Use floor division to maintain integer arithmetic.
        return SizeSuffix(self._size // other_int._size)

    def __rfloordiv__(self, other: "int | SizeSuffix") -> "SizeSuffix":
        other_int = SizeSuffix(other)
        if self._size == 0:
            raise ZeroDivisionError("Division by zero is undefined")
        # Use floor division to maintain integer arithmetic.
        return SizeSuffix(other_int._size // self._size)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SizeSuffix) and not isinstance(other, int):
            return False
        return self._size == SizeSuffix(other)._size

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __lt__(self, other: "int | SizeSuffix") -> bool:
        # if not isinstance(other, SizeSuffix):
        #     return False
        # return self._size < other._size
        return self._size < SizeSuffix(other)._size

    def __le__(self, other: "int | SizeSuffix") -> bool:
        return self._size <= SizeSuffix(other)._size

    def __gt__(self, other: "int | SizeSuffix") -> bool:
        return self._size > SizeSuffix(other)._size

    def __ge__(self, other: "int | SizeSuffix") -> bool:
        return self._size >= SizeSuffix(other)._size

    def __hash__(self) -> int:
        return hash(self._size)

    def __int__(self) -> int:
        return self._size

    def __iadd__(self, other: "int | SizeSuffix") -> Self:
        return type(self)(self._size + SizeSuffix(other)._size)

    def __isub__(self, other: "int | SizeSuffix") -> Self:
        return type(self)(self._size - SizeSuffix(other)._size)


_TMP_DIR_ACCESS_LOCK = Lock()


def _clean_old_files(out: Path) -> None:
    # clean up files older than 1 day
    from rclone_kit.util import locked_print

    now = time.time()
    # Erase all stale files and then purge empty directories.
    for root, _dirs, files in os.walk(out):
        for name in files:
            f = Path(root) / name
            filemod = f.stat().st_mtime
            diff_secs = now - filemod
            diff_days = diff_secs / (60 * 60 * 24)
            if diff_days > 1:
                locked_print(f"Removing old file: {f}")
                f.unlink()

    for root, dirs, _ in os.walk(out):
        for dir in dirs:
            d = Path(root) / dir
            if not list(d.iterdir()):
                locked_print(f"Removing empty directory: {d}")
                d.rmdir()


def get_chunk_tmpdir() -> Path:
    with _TMP_DIR_ACCESS_LOCK:
        dat = get_chunk_tmpdir.__dict__
        if "out" in dat:
            return dat["out"]  # Folder already validated.
        out = Path("chunk_store")
        if out.exists():
            # first access, clean up directory
            _clean_old_files(out)
        out.mkdir(exist_ok=True, parents=True)
        dat["out"] = out
        return out


class EndOfStream:
    pass


@dataclass(frozen=True, slots=True, init=False)
class Range:
    start: SizeSuffix
    end: SizeSuffix

    def __init__(self, start: int | SizeSuffix, end: int | SizeSuffix):
        parsed_start = SizeSuffix(start)
        parsed_end = SizeSuffix(end)
        if parsed_start < 0:
            raise ValueError("Range start must not be negative")
        if parsed_end <= parsed_start:
            raise ValueError("Range end must be greater than start")
        object.__setattr__(self, "start", parsed_start)
        object.__setattr__(self, "end", parsed_end)

    def to_header(self) -> dict[str, str]:
        last = self.end - 1
        val = f"bytes={self.start.as_int()}-{last.as_int()}"
        return {"Range": val}

    def __repr__(self) -> str:
        length = self.end - self.start
        return f"Range(start={self.start}, length={length})"

    def __str__(self) -> str:
        return self.__repr__()


_MAX_PART_NUMBER = 10000


def _get_chunk_size(src_size: int | SizeSuffix, target_chunk_size: int | SizeSuffix) -> SizeSuffix:
    src_size = SizeSuffix(src_size)
    target_chunk_size = SizeSuffix(target_chunk_size)
    if src_size < 0:
        raise ValueError("Source size must not be negative")
    if target_chunk_size <= 0:
        raise ValueError("Target chunk size must be greater than zero")
    minimum_bytes = (src_size.as_int() + _MAX_PART_NUMBER - 1) // _MAX_PART_NUMBER
    min_chunk_size = SizeSuffix(minimum_bytes)
    if min_chunk_size > target_chunk_size:
        warnings.warn(
            f"min_chunk_size: {min_chunk_size} is greater than target_chunk_size: {target_chunk_size}, adjusting target_chunk_size to min_chunk_size",
            stacklevel=2,
        )
        chunk_size = SizeSuffix(min_chunk_size)
    else:
        chunk_size = SizeSuffix(target_chunk_size)
    return chunk_size


def _create_part_infos(
    src_size: int | SizeSuffix, target_chunk_size: int | SizeSuffix
) -> list["PartInfo"]:
    target_chunk_size = SizeSuffix(target_chunk_size)
    src_size = SizeSuffix(src_size)
    if src_size < 0:
        raise ValueError("Source size must not be negative")
    if src_size == 0:
        return []
    chunk_size = _get_chunk_size(src_size=src_size, target_chunk_size=target_chunk_size)

    part_infos: list[PartInfo] = []
    curr_offset: int = 0
    part_number: int = 0
    while True:
        part_number += 1
        done = False
        end = curr_offset + chunk_size
        if end > src_size:
            done = True
            chunk_size = src_size - curr_offset
        range: Range = Range(start=curr_offset, end=curr_offset + chunk_size)
        part_info = PartInfo(
            part_number=part_number,
            range=range,
        )
        part_infos.append(part_info)
        curr_offset += chunk_size.as_int()
        if curr_offset >= src_size:
            break
        if done:
            break
    return part_infos


@dataclass(frozen=True, slots=True)
class PartInfo:
    part_number: int
    range: Range

    @staticmethod
    def split_parts(
        size: int | SizeSuffix, target_chunk_size: int | SizeSuffix
    ) -> list["PartInfo"]:
        out = _create_part_infos(size, target_chunk_size)
        return out

    def __post_init__(self) -> None:
        if not 1 <= self.part_number <= _MAX_PART_NUMBER:
            raise ValueError(f"Part number must be between 1 and {_MAX_PART_NUMBER}")

    @property
    def name(self) -> str:
        partnumber = f"{self.part_number:05d}"
        offset = self.range.start.as_int()
        end = SizeSuffix(self.range.end._size).as_int()
        dst_name = f"part.{partnumber}_{offset}-{end}"
        return dst_name

    def __repr__(self) -> str:
        return f"PartInfo(part_number={self.part_number}, range={self.range})"

    def __str__(self) -> str:
        return self.__repr__()
