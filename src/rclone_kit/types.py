import re
import warnings
from dataclasses import dataclass
from enum import Enum
from typing import Self

_MIN_S3_PATH_PARTS = 2


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
        if ":" not in src:
            raise ValueError(f"Invalid S3 path: {src}")

        remote, path = src.split(":", 1)
        parts = [part.strip() for part in path.split("/") if part.strip()]
        if len(parts) < _MIN_S3_PATH_PARTS:
            raise ValueError(f"Invalid S3 path: {path}")
        bucket = parts[0]
        key = "/".join(parts[1:])
        return S3PathInfo(remote=remote, bucket=bucket, key=key)


@dataclass
class SizeResult:
    """Size result dataclass."""

    prefix: str
    total_size: int
    file_sizes: dict[str, int]


_SIZE_SUFFIX_STEP = 1024
_SIZE_SUFFIX_UNITS: tuple[str, ...] = ("B", "K", "M", "G", "T", "P")
_SIZE_SUFFIX_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)([A-Za-z]+)$")


@dataclass(frozen=True, slots=True, init=False)
class SizeSuffix:
    """A byte count with an rclone-style human-readable suffix (`1.5M`, `16K`, ...)."""

    _size: int

    def __init__(self, size: "int | str | SizeSuffix"):
        parsed_size: int
        if isinstance(size, SizeSuffix):
            parsed_size = size._size
        elif isinstance(size, int):
            parsed_size = size
        elif isinstance(size, str):
            parsed_size = self._parse_size_suffix(size)
        elif isinstance(size, float):
            parsed_size = int(size)
        else:
            raise ValueError(f"Invalid type for size: {type(size)}")
        object.__setattr__(self, "_size", parsed_size)

    @staticmethod
    def _unit_multiplier(unit_index: int) -> int:
        return _SIZE_SUFFIX_STEP**unit_index

    @staticmethod
    def _parse_number_and_unit(value: str) -> tuple[str, str] | None:
        match = _SIZE_SUFFIX_PATTERN.match(value)
        if match is None:
            return None
        return match.group(1), match.group(2)

    @classmethod
    def _parse_size_suffix(cls, size: str) -> int:
        if size == "0":
            return 0
        pair = cls._parse_number_and_unit(size)
        if pair is None:
            raise ValueError(f"Invalid size suffix: {size}")
        num_str, suffix = pair
        unit = suffix[0].upper()
        if unit not in _SIZE_SUFFIX_UNITS:
            raise ValueError(f"Invalid size suffix: {suffix}")
        unit_index = _SIZE_SUFFIX_UNITS.index(unit)
        return int(float(num_str) * cls._unit_multiplier(unit_index))

    def as_int(self) -> int:
        return self._size

    def as_str(self) -> str:
        return self._round_trip_format(self._size)

    @classmethod
    def _round_trip_format(cls, size: int) -> str:
        """Format `size` bytes, then reparse and reformat once more.

        Rounding to one decimal place can push a value across a unit
        boundary (e.g. 1023.9999...K displays as "1024.0K", which is
        really 1.0M); reparsing the first formatted string and
        reconverting the resulting byte count picks the unit that
        actually matches what gets displayed.
        """
        value, unit = cls._value_and_unit(size)
        formatted = cls._format_value(value, unit)
        rounded_size = cls._parse_size_suffix(formatted)
        value, unit = cls._value_and_unit(rounded_size)
        return cls._format_value(value, unit)

    @classmethod
    def _value_and_unit(cls, size: int) -> tuple[float, str]:
        for unit_index, unit in enumerate(_SIZE_SUFFIX_UNITS):
            if size < cls._unit_multiplier(unit_index + 1):
                return size / cls._unit_multiplier(unit_index), unit
        raise ValueError(f"Invalid size: {size}")

    @staticmethod
    def _format_value(value: float, unit: str) -> str:
        number = str(int(value)) if float(value).is_integer() else f"{value:.1f}"
        return f"{number}{unit}"

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

    def __floordiv__(self, other: "int | SizeSuffix") -> "SizeSuffix":
        other_int = SizeSuffix(other)
        if other_int._size == 0:
            raise ZeroDivisionError("Division by zero is undefined")

        return SizeSuffix(self._size // other_int._size)

    def __rfloordiv__(self, other: "int | SizeSuffix") -> "SizeSuffix":
        other_int = SizeSuffix(other)
        if self._size == 0:
            raise ZeroDivisionError("Division by zero is undefined")

        return SizeSuffix(other_int._size // self._size)

    # rclone-kit sizes are always integral bytes, so "true" division is
    # floor division here too.
    __truediv__ = __floordiv__
    __rtruediv__ = __rfloordiv__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SizeSuffix) and not isinstance(other, int):
            return False
        return self._size == SizeSuffix(other)._size

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __lt__(self, other: "int | SizeSuffix") -> bool:

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


@dataclass(frozen=True, slots=True)
class PartInfo:
    part_number: int
    range: Range

    @staticmethod
    def split_parts(
        size: int | SizeSuffix, target_chunk_size: int | SizeSuffix
    ) -> list["PartInfo"]:
        return PartInfo._create_part_infos(size, target_chunk_size)

    @staticmethod
    def _get_chunk_size(
        src_size: int | SizeSuffix, target_chunk_size: int | SizeSuffix
    ) -> SizeSuffix:
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

    @staticmethod
    def _create_part_infos(
        src_size: int | SizeSuffix, target_chunk_size: int | SizeSuffix
    ) -> list["PartInfo"]:
        target_chunk_size = SizeSuffix(target_chunk_size)
        src_size = SizeSuffix(src_size)
        if src_size < 0:
            raise ValueError("Source size must not be negative")
        if src_size == 0:
            return []
        chunk_size = PartInfo._get_chunk_size(
            src_size=src_size, target_chunk_size=target_chunk_size
        )

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
            part_range = Range(start=curr_offset, end=curr_offset + chunk_size)
            part_info = PartInfo(
                part_number=part_number,
                range=part_range,
            )
            part_infos.append(part_info)
            curr_offset += chunk_size.as_int()
            if curr_offset >= src_size:
                break
            if done:
                break
        return part_infos

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
