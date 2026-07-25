from dataclasses import dataclass
from enum import Enum


class DiffType(Enum):
    EQUAL = "="
    MISSING_ON_SRC = "-"
    MISSING_ON_DST = "+"
    DIFFERENT = "*"
    ERROR = "!"


class DiffOption(Enum):
    COMBINED = "combined"
    MISSING_ON_SRC = "missing-on-src"
    MISSING_ON_DST = "missing-on-dst"
    DIFFER = "differ"
    MATCH = "match"
    ERROR = "error"


@dataclass
class DiffItem:
    type: DiffType
    path: str
    src_prefix: str
    dst_prefix: str

    def __str__(self) -> str:
        return f"{self.type.value} {self.path}"

    def __repr__(self) -> str:
        return f"{self.type.name} {self.path}"

    def full_str(self) -> str:
        return f"{self.type.name} {self.src_prefix}/{self.path} {self.dst_prefix}/{self.path}"

    def dst_path(self) -> str:
        return f"{self.dst_prefix}/{self.path}"

    def src_path(self) -> str:
        return f"{self.src_prefix}/{self.path}"
