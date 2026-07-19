import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict

from rclone_kit.types import EndOfStream


class FinishedPieceJson(TypedDict):
    """Shape of one completed part, matching S3's `CompletedPart`."""

    PartNumber: int
    ETag: str


@dataclass
class FinishedPiece:
    part_number: int
    etag: str

    def to_json(self) -> FinishedPieceJson:
        return {"PartNumber": self.part_number, "ETag": self.etag}

    def __post_init__(self):
        assert isinstance(self.part_number, int)
        assert isinstance(self.etag, str)

    @staticmethod
    def to_json_array(
        parts: list["FinishedPiece | EndOfStream"] | list["FinishedPiece"],
    ) -> list[FinishedPieceJson]:
        non_none: list[FinishedPiece] = [p for p in parts if not isinstance(p, EndOfStream)]
        non_none.sort(key=lambda x: x.part_number)

        count_eos = 0
        for p in parts:
            if isinstance(p, EndOfStream):
                count_eos += 1

        if count_eos > 1:
            warnings.warn(
                f"Only one EndOfStream should be present, found {count_eos}", stacklevel=2
            )
        out = [p.to_json() for p in non_none]
        return out

    @staticmethod
    def from_json(data: Mapping[str, object] | None) -> "FinishedPiece | EndOfStream":
        if data is None:
            return EndOfStream()
        part_number = data.get("PartNumber") or data.get("part_number")
        etag = data.get("ETag") or data.get("etag")
        assert isinstance(etag, str)

        etag = etag.replace('"', "")
        assert isinstance(part_number, int)
        assert isinstance(etag, str)
        return FinishedPiece(part_number=part_number, etag=etag)

    @staticmethod
    def from_json_array(data: Sequence[Mapping[str, object] | None]) -> list["FinishedPiece"]:
        tmp = [FinishedPiece.from_json(j) for j in data]
        return [t for t in tmp if isinstance(t, FinishedPiece)]

    def __hash__(self) -> int:
        return hash(self.part_number)
