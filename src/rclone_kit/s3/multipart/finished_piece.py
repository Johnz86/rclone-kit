import warnings
from dataclasses import dataclass

from rclone_kit.types import EndOfStream


@dataclass
class FinishedPiece:
    part_number: int
    etag: str

    def to_json(self) -> dict:

        tag = self.etag
        if not tag.startswith('"'):
            tag = f'"{tag}"'
        out = {"PartNumber": self.part_number, "ETag": self.etag}
        return out

    def __post_init__(self):
        assert isinstance(self.part_number, int)
        assert isinstance(self.etag, str)

    @staticmethod
    def to_json_array(
        parts: list["FinishedPiece | EndOfStream"] | list["FinishedPiece"],
    ) -> list[dict]:
        non_none: list[FinishedPiece] = [p for p in parts if not isinstance(p, EndOfStream)]
        non_none.sort(key=lambda x: x.part_number)

        count_eos = 0
        for p in parts:
            if p is EndOfStream:
                count_eos += 1

        if count_eos > 1:
            warnings.warn(
                f"Only one EndOfStream should be present, found {count_eos}", stacklevel=2
            )
        out = [p.to_json() for p in non_none]
        return out

    @staticmethod
    def from_json(json: dict | None) -> "FinishedPiece | EndOfStream":
        if json is None:
            return EndOfStream()
        part_number = json.get("PartNumber") or json.get("part_number")
        etag = json.get("ETag") or json.get("etag")
        assert isinstance(etag, str)

        etag = etag.replace('"', "")
        assert isinstance(part_number, int)
        assert isinstance(etag, str)
        return FinishedPiece(part_number=part_number, etag=etag)

    @staticmethod
    def from_json_array(json: dict) -> list["FinishedPiece"]:
        tmp = [FinishedPiece.from_json(j) for j in json]
        return [t for t in tmp if isinstance(t, FinishedPiece)]

    def __hash__(self) -> int:
        return hash(self.part_number)
